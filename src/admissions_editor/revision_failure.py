"""Privacy-preserving revision failure records and stored-input recovery.

Only known codes select public messages. Exception strings and provider replies
can contain author text, so they never enter this diagnostic surface.
"""

import copy
import re

VERSION = "revision-failure-1"
STAGES = {"source_review", "feedback_plan", "prepare_plan", "sop_revise", "polish", "humanize",
          "verify_revision", "publish_revision", "unknown"}
MESSAGES = {
    "feedback_interpretation_invalid": "整体评价的模型解析未通过校验，尚未开始生成文章。原评价和补充材料已保留，请核对后重新提交整体评价；无需重新审稿。",
    "feedback_excerpt_invalid": "整体评价中的部分补充信息未能核对为明确的作者陈述。请把事实与修改要求分开说明后重试；原评价已保留，无需重新审稿。",
    "feedback_decisions_invalid": "模型未能将整体评价完整对应到本轮问题。请保留原评价后重新生成；无需重新审稿。",
    "feedback_skills_invalid": "模型返回的编辑技能选择不符合本应用配置。请重新生成；无需重新审稿。",
    "source_changed": "原审稿版本已改变，无法继续这次修改计划。请回到原审稿结果重新提交整体评价。",
    "plan_validation_failed": "修改计划未通过版本或内容校验。原评价和素材已保留，请回到原审稿结果重新提交；无需重复已完成的审稿。",
    "revision_model_failed": "写作模型请求未完成。原评价和素材已保留，请稍后重试生成；无需重新审稿。",
    "revision_storage_failed": "文章处理结果未能完整保存。原评价和素材已保留，请稍后刷新或重试生成。",
    "workflow_interrupted": "全文修订工作流已中断。原评价和素材已保留，请重新提交生成；无需重新审稿。",
    "revision_failed": "全文修订未完成，尚无可下载的优化稿。原评价和补充材料已保留，请回到原审稿结果重试生成；无需重新审稿。",
}
# Equality against application-owned constants is safe; neither the exception
# text nor any interpolated quote is copied into the saved/public diagnostic.
KNOWN_VALIDATION_CODES = {
    "Answer excerpts for a finding must be distinct and non-overlapping.": "feedback_excerpt_invalid",
    "Answer excerpts must quote separate source passages.": "feedback_excerpt_invalid",
    "A confirmed answer must quote an explicit assertion in the author's feedback.": "feedback_excerpt_invalid",
    "Feedback interpretation must decide every original finding exactly once.": "feedback_decisions_invalid",
    "Invalid global decision IDs.": "feedback_decisions_invalid",
    "Invalid selected editorial skills or interpretation.": "feedback_interpretation_invalid",
    "Global feedback does not match its source hashes.": "plan_validation_failed",
}


class RevisionFailure(ValueError):
    """A host failure with an allowlisted public code, never arbitrary prose."""

    def __init__(self, code, stage="unknown"):
        self.failure_code = code
        self.failure_stage = stage
        super().__init__("Revision processing failed.")


def failure_diagnostic(error=None, stage="unknown", *, code=None):
    stage = getattr(error, "failure_stage", stage)
    stage = stage if isinstance(stage, str) and stage in STAGES else "unknown"
    candidate = code or getattr(error, "failure_code", None) or getattr(error, "code", None)
    if not candidate and isinstance(error, ValueError):
        candidate = KNOWN_VALIDATION_CODES.get(str(error))
    if not isinstance(candidate, str) or candidate not in MESSAGES:
        if type(error).__name__ in {"ReviewerError", "TimeoutError", "ConnectionError"}:
            candidate = "revision_model_failed"
        elif isinstance(error, ValueError) or (stage == "feedback_plan" and type(error).__name__ in {"ValidationError", "TypeError", "KeyError"}):
            candidate = "feedback_interpretation_invalid" if stage == "feedback_plan" else "plan_validation_failed"
        elif stage == "publish_revision":
            candidate = "revision_storage_failed"
        elif stage in {"sop_revise", "polish", "humanize", "verify_revision"}:
            candidate = "revision_model_failed"
        else:
            candidate = "revision_failed"
    error_type = type(error).__name__ if error is not None else "UnknownError"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type):
        error_type = "UnknownError"
    return {"schema_version": VERSION, "stage": stage, "code": candidate,
            "user_message": MESSAGES[candidate], "error_type": error_type}


def validate_failure_diagnostic(value):
    """Read stored diagnostics without accepting arbitrary stored message text."""
    if not isinstance(value, dict) or value.get("schema_version") != VERSION or not isinstance(value.get("code"), str) or value["code"] not in MESSAGES or not isinstance(value.get("stage"), str) or value["stage"] not in STAGES:
        return failure_diagnostic()
    result = failure_diagnostic(stage=value["stage"], code=value["code"])
    error_type = value.get("error_type", "")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type):
        result["error_type"] = error_type
    return result


def failed_revision_result(inputs, source_review_id, source_sha256=None, diagnostic=None):
    """Restore only author fields from the server's frozen revision inputs."""
    diagnostic = validate_failure_diagnostic(diagnostic)
    inputs = inputs if isinstance(inputs, dict) else {}
    request = inputs.get("feedback_request")
    request = request if isinstance(request, dict) else {}
    options = {key: copy.deepcopy(request[key]) for key in
               ("essay_type", "word_limit", "writing_sample", "glossary", "confirmed_facts") if key in request}
    message = diagnostic["user_message"]
    result = {"status": "failed", "diagnostic": diagnostic, "feedback_options": options,
              "source_review_id": source_review_id, "review_job_id": source_review_id,
              "run_sha256": source_sha256, "blockers": [message],
              "validation": {"passed": False, "blockers": [message]},
              "stages": [], "trace": [], "artifacts": [], "requires_feedback_resubmission": True}
    if isinstance(request.get("feedback"), str):
        result["feedback"] = request["feedback"]
    report = inputs.get("editorial_report")
    if isinstance(report, dict) and isinstance(report.get("report_sha256"), str):
        result["editorial_report_sha256"] = report["report_sha256"]
    if isinstance(inputs.get("plan"), dict):
        result["plan"] = copy.deepcopy(inputs["plan"])
    return result
