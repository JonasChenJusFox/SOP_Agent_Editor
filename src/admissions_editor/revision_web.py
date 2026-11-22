"""Shared Web adapters for author decisions and full-revision acceptance."""

import copy
import json

from .review import fingerprint, sha256, unified_diff
from .webinput import WebError


def prepare_web_plan(run_text, body=None, *, knowledge_context=None):
    from .revision import prepare_revision_plan
    run_sha = sha256(run_text.encode())
    if body is not None and body.get("run_sha256") != run_sha:
        raise WebError(409, "审稿版本已改变，请刷新并重新准备全文修改计划。")
    data = body or {}
    answers = data.get("answers", {})
    if isinstance(answers, list):
        confirmed = {}
        for answer in answers:
            if not isinstance(answer, dict):
                raise WebError(400, "作者回答格式不正确。")
            if answer.get("confirmed") is True:
                key = answer.get("finding_id")
                if not isinstance(key, str) or key in confirmed:
                    raise WebError(400, "每项问题只能提供一份已确认回答。")
                confirmed[key] = answer.get("answer")
        answers = confirmed
    try:
        return prepare_revision_plan(json.loads(run_text), run_sha,
            decisions=data.get("decisions"), confirmed_facts=data.get("confirmed_facts", []), answers=answers,
            essay_type=data.get("essay_type", "sop"), writing_sample=data.get("writing_sample", ""),
            word_limit=data.get("word_limit"), glossary=data.get("glossary"), knowledge_context=knowledge_context)
    except (ValueError, TypeError, KeyError) as error:
        if isinstance(error, ValueError):
            raise WebError(400, str(error)) from None
        raise WebError(400, "修改计划格式不正确，请核对逐项决定和作者确认素材。") from None


def validate_web_plan(run_text, body):
    from .revision import validate_revision_plan
    run_sha = sha256(run_text.encode())
    plan = body.get("plan")
    if not isinstance(plan, dict) or body.get("run_sha256") != run_sha or plan.get("review_sha256") != run_sha:
        raise WebError(409, "全文修改计划与审稿版本不符，请重新准备计划。")
    try:
        validate_revision_plan(json.loads(run_text), plan)
    except (ValueError, TypeError, KeyError):
        raise WebError(409, "修改计划已过期或内容校验失败，请重新准备计划。") from None
    return plan


def _legacy_revision(record):
    """Authenticate historical structure for display, never current release.

    Old source-policy/prompt bodies are not executed or treated as verified by
    the current engine. Keep author inputs recoverable without approving them.
    """
    from .workflow import verify_run
    if record.get("schema_version") != "full-revision-3" or record["plan"].get("schema_version") != "full-revision-3":
        raise ValueError("Unsupported historical revision.")
    for value, field in ((record, "record_sha256"), (record["plan"], "plan_sha256")):
        if value.get(field) != fingerprint({key: item for key, item in value.items() if key != field}):
            raise ValueError("Historical revision checksum differs.")
    run, plan = record["source_review"], record["plan"]
    verify_run(run)
    if plan["review_content_sha256"] != fingerprint(run) or plan["draft_sha256"] != run["draft_sha256"] or \
            plan["input_sha256"] != run["input_sha256"] or plan["evidence_sha256"] != run["evidence_sha256"] or \
            record["candidate_sha256"] != sha256(record["candidate"].encode()) or \
            record["full_diff"] != unified_diff(run["snapshot"]["draft"], record["candidate"]):
        raise ValueError("Historical revision source or candidate differs.")


def revision_result(record, review_job_id, artifact_names):
    from .revision import REVISION_VERSION, verify_revision
    legacy = record.get("schema_version") == "full-revision-3" and REVISION_VERSION != "full-revision-3"
    try:
        if legacy:
            _legacy_revision(record)
        else:
            verify_revision(record)
    except (ValueError, TypeError, KeyError):
        raise WebError(409, "全文修订记录与当前输入或技能版本不符，请重新生成。") from None
    result = {key: copy.deepcopy(record[key]) for key in ("status", "candidate_sha256", "plan", "validation", "trace", "stages", "record_sha256")}
    result.update(candidate_text=record["candidate"], plan_sha256=record["plan"]["plan_sha256"],
                  technical_context=record["plan"]["technical_context"],
                  technical_accuracy=record["technical_accuracy"],
                  knowledge_context=record["plan"].get("knowledge_context"),
                  blockers=list(record["validation"]["blockers"]), review_job_id=review_job_id,
                  source_review_id=review_job_id, artifacts=sorted(artifact_names))
    if legacy:
        reason = "这是旧版候选记录；作者材料已保留，请重新准备修订计划并生成，旧版结果不能直接接受。"
        result.update(status="blocked", legacy_read_only=True, requires_replan=True)
        result["blockers"].insert(0, reason)
        result["validation"]["passed"] = False
        result["validation"]["blockers"] = list(result["blockers"])
    elif record["status"] == "ready":
        result["final_text"] = record["candidate"]
    return result


def finalize_web(record, body):
    from .revision import finalize_revision
    if not isinstance(body.get("candidate_sha256"), str) or not isinstance(body.get("plan_sha256"), str):
        raise WebError(400, "接受全文需要绑定候选全文与修改计划版本。")
    try:
        return finalize_revision(record, body["candidate_sha256"], body["plan_sha256"])
    except (ValueError, TypeError, KeyError):
        raise WebError(409, "全文核验未通过或接受版本已过期，请检查候选稿、Diff 和阻塞项。") from None
