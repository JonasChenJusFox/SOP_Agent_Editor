"""Small same-origin HTTP interface over the existing review and approval code."""

import copy
import json
import os
import secrets
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cli import approve_command, json_bytes, write_review
from .config import load_config, review_provider, task_provider, revision_provider
from .facts import build_ledger
from .providers import FixtureProvider, RoutedProvider, ROLE_NAMES
from .review import paragraph_spans, read_evidence, run_review, sha256
from .workflow import WORKFLOW_PROMPT_VERSION, RuleWorkflowProvider, enrich_run, retry_adjudication

from .webinput import MAX_BODY, MAX_TEXT, WebError, normalize_input
from .revision_web import prepare_web_plan, validate_web_plan, revision_result, finalize_web
from .editorial import (build_editorial_report, validate_editorial_report, render_editorial_report,
    validate_feedback_request, prepare_feedback_plan, feedback_result_fields)
from .revision_failure import failure_diagnostic, failed_revision_result
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/documents.js": ("documents.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
TERMINAL = {"complete", "partial", "failed"}




def private_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(json_bytes(value))
    temporary.chmod(0o600)
    temporary.replace(path)




class TrackedProvider:
    def __init__(self, provider, app, job_id, on_stage=None):
        self.provider, self.app, self.job_id = provider, app, job_id
        self.returned = set()
        self.lock = threading.Lock()
        self.on_stage = on_stage

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def review(self, role, payload):
        try:
            return self.provider.review(role, payload)
        finally:
            with self.lock:
                self.returned.add(role)
                self.app.update(self.job_id, stage=f"独立审稿：已返回 {len(self.returned)}/4 个角色")

    def task(self, task, payload, schema):
        if self.on_stage is not None:
            self.on_stage(task)
        names = {"observe": "裁决第一阶段：独立观察", "decide": "裁决第二阶段：逐组决定",
                 "editorial_report": "生成综合建议报告", "feedback_plan": "解析整体评价并选择写作 skills",
                 "sop_revise": "全文修订：结构与内容", "polish": "语言润色", "humanize": "Humanizer：最后编辑", "verify_revision": "只读核验：技术含义、事实、要求与采纳覆盖"}
        self.app.update(self.job_id, stage=names.get(task, "检查中"))
        return self.provider.task(task, payload, schema)


class WebApp:
    def __init__(self, data_dir, config_path=None, password=None, examples_dir=EXAMPLES):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.examples = Path(examples_dir)
        self.password = password
        self.config = load_config(config_path) if config_path else {"review": {"provider": "openai"}, "workflow": {"provider": "openai"}}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="web-review")
        self.sessions = {}
        self.jobs = {}
        self.login_attempts = {}
        self.configured_available = True
        try:
            provider = self.providers()[0]
            self.models = {r: provider.label_for(r) for r in ROLE_NAMES}
        except (ValueError, OSError):
            self.configured_available = False
            self.models = {}
        try:
            revision_provider(self.config)
            self.configured_revision_available = True
        except (ValueError, OSError):
            self.configured_revision_available = False
        for path in self.root.glob("*/job.json"):
            try:
                job = json.loads(path.read_text())
                if job["id"] != path.parent.name:
                    continue
                if job["state"] not in TERMINAL:
                    job.update(state="failed", stage="服务已重启，请重新提交。")
                    private_json(path, job)
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def providers(self):
        args = Namespace(provider=None, fixtures=None, model=None)
        return review_provider(args, copy.deepcopy(self.config)), task_provider(self.config)

    def knowledge(self, draft="", *, demo=False):
        from .knowledge_sources import prepare_knowledge
        return prepare_knowledge(draft, self.config, disabled=demo, root=self.examples.parent)

    def knowledge_status(self):
        try:
            context = self.knowledge()
            return {key: context[key] for key in ("status", "sources", "diagnostics") if key in context}
        except (OSError, ValueError) as error:
            return {"status": "unavailable", "sources": [],
                    "diagnostics": [f"本地项目知识暂不可用（{type(error).__name__}），请检查默认来源并刷新索引。"]}

    def assert_knowledge_current(self, context):
        if not context:
            return
        from .knowledge_sources import assert_knowledge_current
        try:
            assert_knowledge_current(context, self.config, root=self.examples.parent)
        except (OSError, ValueError) as error:
            raise WebError(409, "项目来源已改变、缺失或索引过期，请刷新来源并重新审稿/准备计划。" +
                           (" " + str(error) if isinstance(error, ValueError) else "")) from None

    def demo(self):
        try:
            return {"draft": (self.examples / "draft.txt").read_text(),
                    "requirements": (self.examples / "requirements.txt").read_text(),
                    "preferences": (self.examples / "preferences.txt").read_text(),
                    "evidence": [{"id": k, "text": v} for k, v in read_evidence(self.examples / "evidence").items()],
                    "claims": json.loads((self.examples / "claims.json").read_text()),
                    "sources": json.loads((self.examples / "sources.json").read_text()), "as_of": "2026-11-18"}
        except (OSError, ValueError):
            raise WebError(503, "此安装未包含离线示例。") from None

    def revision_demo(self):
        from .cloud_runtime import revision_demo_payload
        return revision_demo_payload(self.examples)

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)
            private_json(self.root / job_id / "job.json", self.jobs[job_id])

    def submit(self, payload, mode="configured", source_id=None, expected_run_sha=None):
        if mode not in {"demo", "configured"}:
            raise WebError(400, "审稿模式不正确。")
        if source_id is None:
            if mode == "demo":
                from .cloud_runtime import prepare_input
                prepare_input(payload, mode, self.examples)
            elif not self.configured_available:
                raise WebError(503, "服务端尚未配置可用模型。请加载 OPENAI_API_KEY 和 OPENAI_MODEL 后启动服务。")
            normalized = normalize_input(payload)
        else:
            source = self.read_run(source_id, expected_run_sha)
            if source["adjudication"]["status"] != "failed" or any(r["status"] != "complete" for r in source["reviewers"].values()):
                raise WebError(409, "此运行不能只重试裁决，请重新审稿。")
            normalized = json.loads((self.root / source_id / "input.json").read_text())
            mode = self.jobs[source_id]["mode"]
        with self.lock:
            if sum(j["state"] not in TERMINAL for j in self.jobs.values()) >= 4:
                raise WebError(429, "已有审稿任务运行，请稍后再试。")
            job_id = secrets.token_hex(16)
            folder = self.root / job_id
            folder.mkdir(mode=0o700)
            private_json(folder / "input.json", normalized)
            draft_path = folder / "draft.txt"
            draft_path.write_bytes(normalized["draft"].encode())
            draft_path.chmod(0o600)
            self.jobs[job_id] = {"id": job_id, "state": "queued", "stage": "等待审稿", "mode": mode,
                                 "created_at": datetime.now().astimezone().isoformat()}
            self.update(job_id)
            self.executor.submit(self.execute, job_id, normalized, mode, source_id, expected_run_sha)
        return {"id": job_id, "state": "queued"}

    def execute(self, job_id, inputs, mode, source_id, expected_run_sha):
        try:
            self.update(job_id, state="running", stage="准备输入快照")
            if mode == "demo":
                provider = RoutedProvider({role: FixtureProvider(self.examples / "fixtures") for role in ROLE_NAMES})
                workflow = RuleWorkflowProvider()
            else:
                provider, workflow = self.providers()
            workflow = TrackedProvider(workflow, self, job_id)
            if source_id:
                source = self.read_run(source_id, expected_run_sha)
                run = retry_adjudication(source, workflow, expected_run_sha)
            else:
                knowledge_context = self.knowledge(inputs["draft"], demo=mode == "demo")
                private_json(self.root / job_id / "knowledge-context.json", knowledge_context)
                context = {"claims": inputs["claims"], "source_metadata": inputs["sources"], "as_of": inputs["as_of"],
                           "workflow_provider": workflow.label, "workflow_prompt_version": WORKFLOW_PROMPT_VERSION,
                           "max_rounds": self.config.get("rounds", {}).get("max_rounds", 3), "author_ready": False,
                           "reverse_opinions": False, "parent_run_sha256": None, "author_feedback": None}
                run = run_review(TrackedProvider(provider, self, job_id), inputs["draft"].encode(), inputs["requirements"],
                                 inputs["preferences"], inputs["evidence"], context=context,
                                 max_workers=self.config.get("review", {}).get("max_workers", 4), knowledge_context=knowledge_context)
                run = enrich_run(run, workflow, manifest=inputs["claims"], source_metadata=inputs["sources"],
                                 as_of=inputs["as_of"], max_rounds=context["max_rounds"])
            write_review(self.root / job_id / "review", run)
            source_sha = sha256((self.root / job_id / "review/run.json").read_bytes())
            report = build_editorial_report(run, source_sha,
                workflow if mode != "demo" and str(workflow.label).startswith("openai:") else None,
                generation_source="offline_fixture" if mode == "demo" else "offline_summary")
            private_json(self.root / job_id / "review/editorial-report.json", report)
            report_path = self.root / job_id / "review/editorial-report.md"
            report_path.write_text(render_editorial_report(report))
            report_path.chmod(0o600)
            self.update(job_id, state=run["status"], stage="审稿完成" if run["status"] == "complete" else "部分步骤未完成，请查看诊断")
        except Exception as error:
            self.update(job_id, state="failed", stage=f"任务未完成（{type(error).__name__}），请检查服务端配置。")

    def read_run(self, job_id, expected=None):
        if self.job(job_id).get("kind", "review") != "review":
            raise WebError(404, "该任务不是审稿记录。")
        path = self.root / job_id / "review/run.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise WebError(409, "审稿结果尚未生成。") from None
        if expected is not None and sha256(raw) != expected:
            raise WebError(409, "审稿版本已改变，请刷新结果。")
        return json.loads(raw)

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise WebError(404, "未找到该审稿任务。")
            return copy.deepcopy(self.jobs[job_id])

    def summary(self, job_id):
        job = self.job(job_id)
        if job.get("kind") == "revision":
            if job["state"] in {"complete", "partial"}:
                folder = self.root / job_id / "revision"
                record = json.loads((folder / "revision.json").read_text())
                names = [str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file()]
                job["result"] = revision_result(record, job["source_id"], names)
                job["result"].update(feedback_result_fields(record))
            elif job["state"] == "failed":
                diagnostic = None
                try:
                    diagnostic = json.loads((self.root / job_id / "internal/revision-failure.json").read_text())
                except (OSError, ValueError, TypeError):
                    pass
                inputs = json.loads((self.root / job_id / "input.json").read_text())
                job["result"] = failed_revision_result(inputs, job["source_id"], job.get("source_sha"), diagnostic)
                job["diagnostic"] = job["result"]["diagnostic"]
            return job
        if job["state"] in {"complete", "partial"}:
            run = self.read_run(job_id)
            job["result"] = {k: run[k] for k in ("status", "draft_sha256", "groups", "changes", "diagnostics", "adjudication", "ledger")}
            job["result"]["run_sha256"] = sha256((self.root / job_id / "review/run.json").read_bytes())
            job["result"]["reviewers"] = {role: {"status": r["status"], "diagnostics": r["diagnostics"]} for role, r in run["reviewers"].items()}
            job["result"]["knowledge_context"] = run["snapshot"].get("knowledge_context")
            job["result"]["editorial_report"] = self.editorial_report(job_id)
        return job

    def approve(self, job_id, body):
        self.read_run(job_id, body.get("run_sha256"))
        if not isinstance(body.get("run_sha256"), str) or not isinstance(body.get("draft_sha256"), str):
            raise WebError(400, "审批需要绑定审稿和原稿版本。")
        run = self.read_run(job_id, body["run_sha256"])
        self.assert_knowledge_current(run["snapshot"].get("knowledge_context"))
        if body["draft_sha256"] != run["draft_sha256"]:
            raise WebError(409, "原稿版本不符，请重新审稿。")
        selected = body.get("change_ids")
        if not isinstance(selected, list) or not selected or any(not isinstance(i, str) for i in selected):
            raise WebError(400, "请先勾选要批准的修改。")
        name = "revised-" + secrets.token_hex(8) + ".txt"
        out = self.root / job_id / "outputs" / name
        try:
            approve_command(Namespace(run=self.root / job_id / "review", draft=self.root / job_id / "draft.txt",
                                      change=selected, keep=False, out=out))
        except ValueError:
            raise WebError(409, "审批校验失败；请确认修改 ID、版本及运行完整性。") from None
        return {"revision": f"/api/reviews/{job_id}/files/outputs/{name}",
                "receipt": f"/api/reviews/{job_id}/files/outputs/{name}.approval.json"}

    def artifact(self, job_id, relative):
        self.job(job_id)
        path = self.root / job_id / relative
        base = (self.root / job_id).resolve()
        if not relative.startswith(("review/", "revision/", "outputs/")) or ".." in Path(relative).parts or "\\" in relative or path.is_symlink() or not path.resolve().is_relative_to(base) or not path.is_file():
            raise WebError(404, "未找到该文件。")
        return path

    def revision_plan(self, review_id, body=None):
        run = self.read_run(review_id)
        context = self.knowledge(run["snapshot"]["draft"], demo=self.job(review_id)["mode"] == "demo")
        return prepare_web_plan((self.root / review_id / "review/run.json").read_text(), body, knowledge_context=context)

    def editorial_report(self, review_id):
        run = self.read_run(review_id)
        source_sha = sha256((self.root / review_id / "review/run.json").read_bytes())
        path = self.root / review_id / "review/editorial-report.json"
        if not path.is_file():
            return build_editorial_report(run, source_sha)
        try:
            return validate_editorial_report(run, source_sha, json.loads(path.read_text()))
        except (ValueError, KeyError, TypeError):
            raise WebError(409, "综合报告与当前审稿不符，请重新审稿。") from None

    def submit_revision(self, review_id, body):
        self.read_run(review_id)
        source_text = (self.root / review_id / "review/run.json").read_text()
        plan = validate_web_plan(source_text, body)
        self.assert_knowledge_current(plan.get("knowledge_context"))
        if self.job(review_id)["mode"] != "demo" and not self.configured_revision_available:
            raise WebError(503, "服务端尚未配置全文改稿模型，请检查 revision 配置和环境变量。")
        if sha256((self.root / review_id / "draft.txt").read_bytes()) != plan["draft_sha256"]:
            raise WebError(409, "原稿已经改变，请重新审稿后再生成全文。")
        return self.queue_revision(review_id, source_text, {"plan": plan})

    def submit_feedback_revision(self, review_id, body):
        run = self.read_run(review_id)
        source_text = (self.root / review_id / "review/run.json").read_text()
        report = self.editorial_report(review_id)
        normalized = validate_feedback_request(source_text, report, body)
        if self.job(review_id)["mode"] != "demo" and not self.configured_revision_available:
            raise WebError(503, "服务端尚未配置全文改稿模型。")
        if sha256((self.root / review_id / "draft.txt").read_bytes()) != run["draft_sha256"]:
            raise WebError(409, "原稿已经改变，请重新审稿后再生成全文。")
        context = self.knowledge(run["snapshot"]["draft"], demo=self.job(review_id)["mode"] == "demo")
        self.assert_knowledge_current(context)
        return self.queue_revision(review_id, source_text,
            {"feedback_request": normalized, "editorial_report": report, "knowledge_context": context})

    def queue_revision(self, review_id, source_text, inputs):
        with self.lock:
            if sum(job["state"] not in TERMINAL for job in self.jobs.values()) >= 4:
                raise WebError(429, "已有任务运行，请稍后再试。")
            job_id = secrets.token_hex(16)
            folder = self.root / job_id
            folder.mkdir(mode=0o700)
            private_json(folder / "input.json", inputs)
            self.jobs[job_id] = {"id": job_id, "kind": "revision", "mode": self.jobs[review_id]["mode"],
                                 "source_id": review_id, "source_sha": sha256(source_text.encode()),
                                 "state": "queued", "stage": "等待全文修订", "created_at": datetime.now().astimezone().isoformat()}
            self.update(job_id)
            self.executor.submit(self.execute_revision, job_id, inputs.get("plan"))
        return {"id": job_id, "state": "queued"}

    def execute_revision(self, job_id, plan):
        from .cli import publish_directory
        from .revision import revise_document, revision_artifacts
        active_stage = "source_review"

        def track_stage(stage):
            nonlocal active_stage
            active_stage = stage

        try:
            job = self.job(job_id)
            source = self.read_run(job["source_id"], job["source_sha"])
            active_stage = "feedback_plan" if plan is None else "prepare_plan"
            if job["mode"] == "demo":
                provider = FixtureProvider(self.examples / "revision" / "fixtures")
            else:
                provider = revision_provider(self.config)
            if plan is None:
                inputs = json.loads((self.root / job_id / "input.json").read_text())
                planner = provider.providers["sop_revise"] if hasattr(provider, "providers") else None
                planner = planner if planner and str(planner.label).startswith("openai:") else None
                plan = prepare_feedback_plan(source, job["source_sha"], inputs["editorial_report"], inputs["feedback_request"],
                    TrackedProvider(planner, self, job_id, track_stage) if planner else None,
                    knowledge_context=inputs.get("knowledge_context"), demo_manifest=self.revision_demo() if planner is None else None)
            active_stage = "prepare_plan"
            self.assert_knowledge_current(plan.get("knowledge_context"))
            self.update(job_id, state="running", stage="准备作者确认的全文修改计划")
            record = revise_document(source, plan, TrackedProvider(provider, self, job_id, track_stage))
            active_stage = "publish_revision"
            artifacts = revision_artifacts(record)
            if plan.get("editorial_context"):
                artifacts["feedback-analysis.json"] = json_bytes(plan["editorial_context"])
            publish_directory(self.root / job_id / "revision", artifacts)
            self.update(job_id, state="complete" if record["status"] == "ready" else "partial",
                        stage="全文核验通过，请检查候选和 Diff 后接受" if record["status"] == "ready" else "全文候选有待处理项，请查看核验结果")
        except Exception as error:
            diagnostic = failure_diagnostic(error, active_stage)
            try:
                folder = self.root / job_id / "internal"
                folder.mkdir(mode=0o700, exist_ok=True)
                private_json(folder / "revision-failure.json", diagnostic)
            except (OSError, ValueError, TypeError):
                pass
            self.update(job_id, state="failed", stage=diagnostic["user_message"])

    def finalize_full_revision(self, job_id, body):
        job = self.job(job_id)
        if job.get("kind") != "revision" or job["state"] not in {"complete", "partial"}:
            raise WebError(409, "全文修订尚未生成。")
        record = json.loads((self.root / job_id / "revision/revision.json").read_text())
        self.assert_knowledge_current(record["plan"].get("knowledge_context"))
        self.read_run(job["source_id"], job["source_sha"])
        if sha256((self.root / job["source_id"] / "draft.txt").read_bytes()) != record["plan"]["draft_sha256"]:
            raise WebError(409, "原稿已经改变，不能接受过期的全文修订。")
        text, receipt = finalize_web(record, body)
        from .cli import publish_directory
        name = "accepted-" + secrets.token_hex(8)
        publish_directory(self.root / job_id / "outputs" / name, {"essay.txt": text.encode(), "acceptance.json": json_bytes(receipt)})
        return {"revision": f"/api/revisions/{job_id}/files/outputs/{name}/essay.txt",
                "receipt": f"/api/revisions/{job_id}/files/outputs/{name}/acceptance.json"}

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, WebHandler)


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No request bodies, cookies, tokens or manuscript text in access logs.

    @property
    def app(self):
        return self.server.app

    def send_content(self, status, content, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; worker-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, value, extra=None):
        self.send_content(status, json_bytes(value), extra=extra)

    def authenticated(self):
        hostname = urlsplit("//" + self.headers.get("Host", "")).hostname
        if not self.app.password:
            forwarded = any(self.headers.get(key) for key in ("CF-Ray", "CF-Connecting-IP", "X-Forwarded-For", "X-Forwarded-Proto"))
            return hostname in {"localhost", "127.0.0.1", "::1"} and not forwarded
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        token = cookie.get("admissions_session")
        with self.app.lock:
            return bool(token and self.app.sessions.get(token.value, 0) > time.time())

    def body(self):
        origin = self.headers.get("Origin")
        if origin and (urlsplit(origin).netloc != self.headers.get("Host") or urlsplit(origin).scheme not in {"http", "https"}):
            raise WebError(403, "请求来源不匹配。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WebError(403, "不接受跨站请求。")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise WebError(415, "请使用 JSON 请求。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise WebError(400, "请求长度不正确。") from None
        if not 0 < length <= MAX_BODY:
            raise WebError(413, "一次提交最多 512 KB。")
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise WebError(400, "JSON 请求不正确。") from None
        if not isinstance(value, dict):
            raise WebError(400, "请求需要是 JSON 对象。")
        return value

    def dispatch(self, post=False):
        path = unquote(urlsplit(self.path).path)
        if not post and path in STATIC:
            filename, mime = STATIC[path]
            return self.send_content(200, files("admissions_editor").joinpath("web", filename).read_bytes(), mime)
        if not post and path.startswith("/vendor/"):
            parts = Path(path.lstrip("/")).parts
            if ".." in parts or "\\" in path or Path(path).suffix.lower() not in {".js", ".mjs", ".json", ".txt", ".bcmap", ".pfb", ".ttf", ".otf"}:
                raise WebError(404, "未找到该文件。")
            resource = files("admissions_editor").joinpath("web", *parts)
            if not resource.is_file():
                raise WebError(404, "未找到该文件。")
            mime = "text/javascript; charset=utf-8" if Path(path).suffix in {".js", ".mjs"} else guess_type(path)[0] or "application/octet-stream"
            return self.send_content(200, resource.read_bytes(), mime)
        if not post and path == "/api/session":
            return self.json(200, {"authenticated": self.authenticated(), "password_required": bool(self.app.password)})
        if post and path == "/api/session":
            body = self.body()
            if not self.app.password:
                raise WebError(403, "远程访问前请在服务端配置 ADMISSIONS_WEB_PASSWORD。")
            with self.app.lock:
                attempts = [t for t in self.app.login_attempts.get(self.client_address[0], []) if time.time() - t < 60]
                if len(attempts) >= 8:
                    raise WebError(429, "登录尝试过多，请稍后再试。")
                attempts.append(time.time())
                self.app.login_attempts[self.client_address[0]] = attempts
            password = body.get("password", "")
            if not isinstance(password, str) or not secrets.compare_digest(password.encode(), self.app.password.encode()):
                raise WebError(401, "访问密码不正确。")
            token = secrets.token_urlsafe(32)
            with self.app.lock:
                self.app.sessions[token] = time.time() + 12 * 3600
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self.json(200, {"authenticated": True}, {"Set-Cookie": f"admissions_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{secure}"})
        if not self.authenticated():
            raise WebError(401, "请先输入访问密码；远程访问需要服务端配置 ADMISSIONS_WEB_PASSWORD。")
        if not post and path == "/api/config":
            return self.json(200, {"configured_review_available": self.app.configured_available, "models": self.app.models,
                                   "configured_revision_available": self.app.configured_revision_available,
                                   "knowledge_status": self.app.knowledge_status(),
                                   "max_file_bytes": MAX_TEXT, "max_body_bytes": MAX_BODY})
        if not post and path == "/api/demo":
            return self.json(200, self.app.demo())
        if not post and path == "/api/revision-demo":
            return self.json(200, self.app.revision_demo())
        if not post and path == "/api/skills":
            from .editorial_skills import editorial_manifest
            return self.json(200, {"skills": editorial_manifest()})
        if post and path == "/api/reviews":
            body = self.body()
            return self.json(202, self.app.submit(body.get("input"), body.get("mode", "configured")))
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "reviews"]:
            job_id = parts[2]
            if self.app.job(job_id).get("kind", "review") != "review":
                raise WebError(404, "该任务不是审稿记录。")
            if not post and len(parts) == 3:
                return self.json(200, self.app.summary(job_id))
            if post and parts[3:] == ["approve"]:
                return self.json(201, self.app.approve(job_id, self.body()))
            if post and parts[3:] == ["retry-adjudication"]:
                body = self.body()
                if not isinstance(body.get("run_sha256"), str):
                    raise WebError(400, "裁决重试需要绑定审稿版本。")
                return self.json(202, self.app.submit(None, source_id=job_id, expected_run_sha=body["run_sha256"]))
            if parts[3:] == ["revision-plan"]:
                return self.json(200, {"plan": self.app.revision_plan(job_id, self.body() if post else None)})
            if post and parts[3:] == ["revisions"]:
                return self.json(202, self.app.submit_revision(job_id, self.body()))
            if post and parts[3:] == ["feedback-revisions"]:
                return self.json(202, self.app.submit_feedback_revision(job_id, self.body()))
            if not post and parts[3:] == ["editorial-report"]:
                return self.json(200, {"editorial_report": self.app.editorial_report(job_id)})
            if not post and parts[3:] == ["documents", "editorial-report.docx"]:
                from .word_documents import DOCX_MIME, editorial_document
                source_text = (self.app.root / job_id / "review/run.json").read_text()
                return self.send_content(200, editorial_document(self.app.editorial_report(job_id), json.loads(source_text), sha256(source_text.encode())), DOCX_MIME,
                    {"Content-Disposition": 'attachment; filename="editorial-report.docx"'})
            if not post and len(parts) > 4 and parts[3] == "files":
                file = self.app.artifact(job_id, "/".join(parts[4:]))
                mime = "application/json; charset=utf-8" if file.suffix == ".json" else "text/plain; charset=utf-8"
                return self.send_content(200, file.read_bytes(), mime, {"Content-Disposition": "attachment"})
        if len(parts) >= 3 and parts[:2] == ["api", "revisions"]:
            job_id = parts[2]
            if self.app.job(job_id).get("kind") != "revision":
                raise WebError(404, "未找到该全文修订任务。")
            if not post and len(parts) == 3:
                return self.json(200, self.app.summary(job_id))
            if post and parts[3:] == ["finalize"]:
                return self.json(201, self.app.finalize_full_revision(job_id, self.body()))
            if not post and len(parts) == 5 and parts[3] == "documents" and parts[4] in {"essay.docx", "change-report.docx"}:
                from .word_documents import DOCX_MIME, revision_document
                record_path = self.app.artifact(job_id, "revision/revision.json")
                return self.send_content(200, revision_document(json.loads(record_path.read_text()), parts[4]), DOCX_MIME,
                    {"Content-Disposition": 'attachment; filename="' + parts[4] + '"'})
            if not post and len(parts) > 4 and parts[3] == "files":
                relative = "/".join(parts[4:])
                file = self.app.artifact(job_id, relative if relative.startswith("outputs/") else "revision/" + relative)
                mime = "application/json; charset=utf-8" if file.suffix == ".json" else "text/plain; charset=utf-8"
                return self.send_content(200, file.read_bytes(), mime, {"Content-Disposition": "attachment"})
        raise WebError(404, "未找到该接口。")

    def handle_request(self, post=False):
        try:
            self.dispatch(post)
        except WebError as error:
            self.json(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.json(500, {"error": "服务未完成请求，请检查服务端配置。"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


def serve_command(args):
    password = os.environ.get("ADMISSIONS_WEB_PASSWORD")
    if args.password_file:
        password = args.password_file.read_text(encoding="utf-8").strip()
    if password and (len(password) < 12 or password.startswith(("REPLACE_", "YOUR_"))):
        raise ValueError("Set a real ADMISSIONS_WEB_PASSWORD containing at least 12 characters.")
    if args.host not in {"localhost", "127.0.0.1", "::1"} and not password:
        raise ValueError("Set ADMISSIONS_WEB_PASSWORD before binding a non-loopback interface.")
    app = WebApp(args.data_dir, args.config, password=password)
    server = WebServer((args.host, args.port), app)
    print(f"Admissions Editor: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0
