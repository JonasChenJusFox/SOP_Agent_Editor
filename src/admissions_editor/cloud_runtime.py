"""Replay the existing pipeline around durable, asynchronous model-call steps.

No HTTP, threads, secret access, or persistent filesystem is used here. The host
executes each requested call once and supplies its saved response on the next pass.
Every missing request exits before the core can treat it as a reviewer failure.
"""

import copy
import json
from pathlib import Path

from .cli import approval_artifacts, json_bytes, review_artifacts
from .providers import FixtureProvider, OpenAIProvider, ReviewerError, ROLE_NAMES, RoutedProvider, TaskRoutedProvider
from .review import fingerprint, read_evidence, run_review, sha256
from .webinput import WebError, normalize_input
from .workflow import WORKFLOW_PROMPT_VERSION, RuleWorkflowProvider, enrich_run, retry_adjudication, verify_run


class PendingCall(BaseException):
    """Internal scheduling signal, deliberately outside provider Exception handlers."""

    def __init__(self, call_id, payload, timeout, retries):
        self.action = {"call_id": call_id, "key": fingerprint(payload), "payload": payload,
                       "timeout": timeout, "retries": retries}


class ReplayOpenAI(OpenAIProvider):
    def __init__(self, settings, responses):
        # The host owns API authentication. Inherited methods only build/decode
        # requests; _request_once is never used by this adapter.
        self.model = settings["model"]
        self.label = "openai:" + self.model
        self.timeout = settings.get("timeout", 90)
        self.retries = settings.get("retries", 1)
        self.responses = responses
        self.call_records = {}
        self.focus_override = None
        self._last_attempts = 0
        self.call_id = None

    def review(self, role, payload):
        self.call_id = role
        return super().review(role, payload)

    def task(self, task, payload, schema):
        self.call_id = task
        return super().task(task, payload, schema)

    def _request(self, payload):
        key = fingerprint(payload)
        self.last_request_key = key
        if key not in self.responses:
            raise PendingCall(self.call_id, payload, self.timeout, self.retries)
        saved = self.responses[key]
        self._last_attempts = saved.get("attempts", 1)
        if "error" in saved:
            raise ReviewerError(saved["error"])
        return copy.deepcopy(saved["response"])

    def _metrics(self, response):
        result = super()._metrics(response)
        elapsed = self.responses.get(getattr(self, "last_request_key", None), {}).get("elapsed_seconds")
        if isinstance(elapsed, (int, float)) and elapsed >= 0:
            result["elapsed_seconds"] = elapsed
        return result


def demo_input(examples):
    root = Path(examples)
    return {"draft": (root / "draft.txt").read_text(), "requirements": (root / "requirements.txt").read_text(),
            "preferences": (root / "preferences.txt").read_text(),
            "evidence": [{"id": k, "text": v} for k, v in read_evidence(root / "evidence").items()],
            "claims": json.loads((root / "claims.json").read_text()),
            "sources": json.loads((root / "sources.json").read_text()), "as_of": "2026-11-18"}


def revision_demo_payload(examples):
    try:
        value = json.loads((Path(examples) / "revision/demo.json").read_text())
        if not isinstance(value, dict) or not isinstance(value.get("input"), dict) or not isinstance(value.get("plan_input"), dict):
            raise ValueError("Invalid revision example")
        return value
    except (OSError, ValueError):
        raise WebError(503, "此安装未包含完整改稿的离线示例。") from None


def next_action(inputs, mode, config, responses, examples, source=None, source_sha=None):
    """Return the next exact model request, or the complete validated artifacts."""
    if mode == "demo":
        provider = RoutedProvider({role: FixtureProvider(Path(examples) / "fixtures") for role in ROLE_NAMES})
        workflow = RuleWorkflowProvider()
    else:
        provider = RoutedProvider({role: ReplayOpenAI(config["roles"][role], responses) for role in ROLE_NAMES})
        workflow = ReplayOpenAI(config["workflow"], responses)
    try:
        if source is not None:
            run = retry_adjudication(copy.deepcopy(source), workflow, source_sha)
        else:
            from .knowledge_sources import unavailable_knowledge_context
            knowledge_context = unavailable_knowledge_context(
                "内置合成示例跳过私有项目知识。" if mode == "demo" else
                "Cloudflare 无法访问本机 private/knowledge/repos/；默认项目来源缺失，本次未使用本机代码。不会自动上传代码仓库或 SQLite 索引。",
                disabled=mode == "demo")
            context = {"claims": inputs["claims"], "source_metadata": inputs["sources"], "as_of": inputs["as_of"],
                       "workflow_provider": workflow.label, "workflow_prompt_version": WORKFLOW_PROMPT_VERSION,
                       "max_rounds": config.get("max_rounds", 3), "author_ready": False,
                       "reverse_opinions": False, "parent_run_sha256": None, "author_feedback": None}
            run = run_review(provider, inputs["draft"].encode(), inputs["requirements"], inputs["preferences"],
                             inputs["evidence"], max_workers=1, context=context, knowledge_context=knowledge_context)
            for review in run["reviewers"].values():
                if "elapsed_seconds" in review.get("metrics", {}):
                    review["elapsed_seconds"] = review["metrics"]["elapsed_seconds"]
            run = enrich_run(run, workflow, manifest=inputs["claims"], source_metadata=inputs["sources"],
                             as_of=inputs["as_of"], max_rounds=context["max_rounds"])
        verify_run(run)
        return {"complete": True, "run": run,
                "artifacts": {name: raw.decode() for name, raw in review_artifacts(run).items()}}
    except PendingCall as pending:
        return {"complete": False, **pending.action}


def prepare_input(raw, mode, examples):
    if mode not in {"demo", "configured"}:
        raise WebError(400, "审稿模式不正确。")
    if mode == "demo" and raw != demo_input(examples):
        try:
            alternate = revision_demo_payload(examples)["input"]
        except WebError:
            alternate = None
        if raw != alternate:
            raise WebError(400, "离线演示只支持未修改的内置示例。")
    return normalize_input(raw)


def next_revision_action(run, plan, config, responses, examples, mode="configured"):
    """Drive the pure revision engine using the same durable HTTP bridge."""
    from .revision import revise_document, revision_artifacts
    if mode == "demo":
        provider = FixtureProvider(Path(examples) / "revision/fixtures")
    else:
        provider = TaskRoutedProvider({stage: ReplayOpenAI(settings, responses) for stage, settings in config["revision"].items()})
    try:
        record = revise_document(copy.deepcopy(run), copy.deepcopy(plan), provider)
        return {"complete": True, "record": record,
                "artifacts": {name: content.decode() for name, content in revision_artifacts(record).items()}}
    except PendingCall as pending:
        return {"complete": False, **pending.action}


def approve(run_text, body):
    if not isinstance(body.get("run_sha256"), str) or not isinstance(body.get("draft_sha256"), str):
        raise WebError(400, "审批需要绑定审稿和原稿版本。")
    run = json.loads(run_text)
    if sha256(run_text.encode()) != body["run_sha256"] or run["draft_sha256"] != body["draft_sha256"]:
        raise WebError(409, "审稿或原稿版本不符，请重新审稿。")
    selected = body.get("change_ids")
    if not isinstance(selected, list) or not selected or any(not isinstance(i, str) for i in selected):
        raise WebError(400, "请先勾选要批准的修改。")
    try:
        revision, receipt = approval_artifacts(run_text.encode(), run["snapshot"]["draft"].encode(), selected)
    except ValueError:
        raise WebError(409, "审批校验失败，请确认修改 ID、版本及运行完整性。") from None
    return revision.decode(), receipt.decode()
