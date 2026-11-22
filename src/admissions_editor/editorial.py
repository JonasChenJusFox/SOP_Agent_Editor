"""Consolidated review reports and bounded interpretation of author feedback.

The model maps one author comment into the existing version-bound revision
protocol. It cannot invent findings, install skills, or promote suggestions to
personal evidence. Hosts persist these records alongside the untouched review.
"""

import copy
import json
import re

from .contracts import STRING, array, choice, obj, validate
from .review import fingerprint, sha256
from .webinput import WebError

SKILL_IDS = ("sop_revise", "polish", "humanize")
MAX_FEEDBACK_BYTES = 32 * 1024
REPORT_VERSION = "editorial-report-1"
CONTEXT_VERSION = "editorial-context-1"

TASK_RULES = {
    "editorial_report": (
        "Consolidate the supplied independent review findings into one readable editorial report in Chinese. "
        "Use a short overall summary and at most 8 numbered issue groups. Explain each problem and a concrete "
        "recommendation. Group repeated observations, but cover every supplied finding ID exactly once. "
        "Do not add new findings or facts, treat reviewer agreement as proof, or ask the author to vote on each finding. "
        "A question or absent evidence is uncertainty, not a proven falsehood. Keep technical distinctions and contribution scope."
    ),
    "feedback_plan": (
        "Interpret the author's single overall feedback comment in relation to the consolidated report and original findings. "
        "The author authorizes you to map their overall editorial direction into a complete accept/reject/defer decision "
        "and reason for every exact finding ID. Respect explicit exceptions, numbered-report references, tone and structure "
        "preferences; do not silently accept all. Resolve overlapping actions consistently. Accepted preserve findings retain text. "
        "For factual questions, answer_excerpts may contain one or more distinct, non-overlapping, exact, self-contained "
        "explicit factual assertions per finding, quoted "
        "from the author's feedback. Suggestions, examples, requests, assumptions, uncertainty, future plans and hypothetical "
        "wording cannot become confirmed personal achievements. An explicitly declared educational goal such as 'I want to "
        "study recovery' may be quoted as that goal only, never as a completed qualification, contribution or result. "
        "Do not duplicate or overlap excerpts. Leave absent answers out and explain unresolved questions; preserve "
        "the original contribution wording rather than inventing facts. Select relevant skill IDs only from the supplied local "
        "catalog. Selection adds editorial focus; all mandatory writing and read-only verification stages still run. "
        "Return a concise Chinese interpretation summary, complete decisions, exact answer excerpts, selected_skill_ids and "
        "unresolved_questions. No external skills, services, model identity comparisons or arbitrary tool execution."
    ),
}


def findings_for_report(run):
    from .revision import _finding_rows
    return [{key: copy.deepcopy(row[key]) for key in
             ("finding_id", "kind", "severity", "paragraph_id", "quote", "rationale", "action", "replacement", "evidence_ids")
             if key in row} for row in _finding_rows(run)]


def report_schema(finding_ids):
    reference = choice(*finding_ids) if finding_ids else STRING
    issue = obj({"title": STRING, "problem": STRING, "recommendation": STRING,
                 "finding_ids": {**array(reference), "minItems": 1}})
    return obj({"summary": STRING, "issues": {**array(issue), "maxItems": min(8, len(finding_ids))}})


def offline_report_output(findings):
    groups = {}
    labels = {"structure": "结构与叙事", "admissions": "申请匹配与个人动机",
              "language": "语言与表达", "skeptical": "事实、证据与贡献范围"}
    for finding in findings:
        category = finding["finding_id"].split("-", 1)[0]
        groups.setdefault(category if category in labels else "other", []).append(finding)
    issues = []
    for category, rows in groups.items():
        recommendations = []
        for row in rows:
            if row.get("action") == "preserve":
                recommendations.append("保留已有有效内容：" + row["quote"])
            elif row.get("replacement"):
                recommendations.append("可参考的局部修改：" + row["replacement"])
            elif row.get("action") == "verify" or row.get("kind") == "question":
                recommendations.append("补充已核对的信息；无法确认时保留事实边界，不新增成果或个人职责。")
            else:
                recommendations.append("根据上述问题调整内容和衔接，并保留原有事实与作者语气。")
        issues.append({"title": labels.get(category, "其他需要注意的问题"),
                       "problem": "\n".join(row["rationale"] for row in rows),
                       "recommendation": "\n".join(dict.fromkeys(recommendations)),
                       "finding_ids": [row["finding_id"] for row in rows]})
    return {"summary": "以下按主题归并独立审稿结果。请在一个评价框内说明整体方向、需要保留的内容及已确认的补充信息。" if findings else
            "本轮未报告具体问题。你仍可在整体评价中说明希望调整的语气、结构或重点。", "issues": issues}


def _seal(value, key):
    value[key] = fingerprint({k: v for k, v in value.items() if k != key})
    return value


def build_editorial_report(run, run_sha256, provider=None, *, generation_source="offline_summary"):
    """Model input intentionally excludes timing/run seals so durable replay is stable."""
    from .workflow import verify_run
    verify_run(run)
    findings = findings_for_report(run)
    output = offline_report_output(findings)
    diagnostics = []
    if provider is not None and run["status"] == "complete":
        try:
            output = provider.task("editorial_report", {
                "draft_sha256": run["draft_sha256"], "original": run["snapshot"], "findings": findings,
            }, report_schema([row["finding_id"] for row in findings]))
            generation_source = "model"
            _validate_report_output(output, findings)
        except Exception as error:
            # PendingCall inherits BaseException and must propagate to the host.
            output = offline_report_output(findings)
            generation_source = "model_synthesis_failed"
            diagnostics = ["综合模型报告未完成，当前显示来源明确的审稿归并结果（" + type(error).__name__ + "）。"]
    _validate_report_output(output, findings)
    return _seal({"schema_version": REPORT_VERSION, "review_sha256": run_sha256,
                  "draft_sha256": run["draft_sha256"], "summary": output["summary"],
                  "issues": [{"number": index + 1, **row} for index, row in enumerate(output["issues"])],
                  "generation_source": generation_source, "diagnostics": diagnostics}, "report_sha256")


def _validate_report_output(output, findings):
    ids = [row["finding_id"] for row in findings]
    validate(output, report_schema(ids), "editorial_report")
    cited = [item for issue in output["issues"] for item in issue["finding_ids"]]
    if sorted(cited) != sorted(ids) or not output["summary"].strip() or any(not all(issue[key].strip() for key in
            ("title", "problem", "recommendation")) for issue in output["issues"]):
        raise ValueError("The consolidated report must cover each finding exactly once.")


def validate_editorial_report(run, run_sha256, report):
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_VERSION or \
            report.get("review_sha256") != run_sha256 or report.get("draft_sha256") != run["draft_sha256"] or \
            report.get("report_sha256") != fingerprint({k: v for k, v in report.items() if k != "report_sha256"}):
        raise ValueError("The consolidated report is stale or differs from its review.")
    output = {"summary": report["summary"], "issues": [{k: v for k, v in row.items() if k != "number"} for row in report["issues"]]}
    _validate_report_output(output, findings_for_report(run))
    if [row["number"] for row in report["issues"]] != list(range(1, len(report["issues"]) + 1)):
        raise ValueError("Report numbering differs.")
    return report


def render_editorial_report(report):
    lines = ["# 综合建议报告", "", report["summary"], "", "来源：" + report["generation_source"], ""]
    lines += report.get("diagnostics", [])
    for issue in report["issues"]:
        lines += [f"## {issue['number']}. {issue['title']}", "", issue["problem"], "", "建议：" + issue["recommendation"], ""]
    return "\n".join(lines)


def feedback_schema(finding_ids):
    reference = choice(*finding_ids) if finding_ids else STRING
    return obj({"summary": STRING,
                "decisions": array(obj({"finding_id": reference, "decision": choice("accept", "reject", "defer"), "reason": STRING})),
                "answer_excerpts": array(obj({"finding_id": reference, "quote": STRING})),
                "selected_skill_ids": {**array(choice(*SKILL_IDS)), "minItems": 1},
                "unresolved_questions": array(STRING)})


def skill_catalog():
    from .editorial_skills import editorial_manifest
    descriptions = {"sop_revise": "SOP/PS/短答的内容重点、结构与申请叙事。", "polish": "语法、衔接、精确表达与克制润色。",
                    "humanize": "最后的自然表达检查，保留作者声音及专业术语。"}
    return [{"id": row["stage"], "name": row["name"], "version": row["version"],
             "sha256": row["sha256"], "purpose": descriptions[row["stage"]]}
            for row in editorial_manifest() if row["stage"] in SKILL_IDS]


def _assertion(quote):
    # Fail closed on proposal/hypothesis language. The semantic model still has
    # to judge factual assertion; substring matching alone never proves truth.
    if not isinstance(quote, str) or not quote.strip():
        return False
    uncertain = r"\b(?:if|suppose|supposing|hypothetically|perhaps|maybe|might|could|would|should|will|shall|suggest|imagine|example|please|want|wish|hope|plan|intend|fictional|unconfirmed)\b|\bgoing\s+to\b|\bmy\s+(?:goal|aim)\s+is\s+to\b|(?:如果|假设|例如|比如|可能|或许|大概|希望|建议|请|打算|计划|想要|我将|我会|将要|我的目标是|不确定|能否|是否|未确认|虚构|不属实)|(?:do not|don't)\s+(?:write|claim|add|say|include|state)|不要(?:写|说|增加|声称|添加|加入)"
    value = quote.strip()
    # Declaring a study interest is evidence of that stated intention, not a
    # completed achievement. Remove only this explicit educational-goal prefix
    # for uncertainty checking; an added ownership/result clause still fails.
    educational_goal = re.match(r"(?:I\s+(?:want|hope|intend|aim|plan)\s+to|My\s+(?:goal|aim)\s+is\s+to)\s+(?:study|learn|explore|understand|examine|investigate|research|pursue)\b|"
                                r"(?:我(?:希望|想要|计划|打算|期望)|我的目标是)(?:进一步)?(?:学习|研究|探索|理解|了解|深造)", value, re.I)
    if educational_goal:
        remainder = value[educational_goal.end():]
        achievement = r"\b(?:build|built|develop|developed|train|trained|fine[ -]tune|lead|led|own|owned|deploy|deployed|achieve|achieved|win|won|publish|published|graduate|graduated|earn|earned|independently|solely|single-handedly)\b|(?:开发|训练|构建|搭建|独立|主导|领导|拥有|部署|实现|达成|获得|赢得|发表|毕业|提升|提高)"
        if re.search(achievement, remainder, re.I):
            return False
        value = remainder
    return not re.search(uncertain, value, re.I) and not re.search(r"[?？]", quote)


def answers_from_excerpts(excerpts, feedback=None):
    """Join distinct verbatim excerpts with newlines, adding no invented words.

    The original list remains in the sealed interpretation for provenance.
    Repeated/nested excerpts cannot inflate an answer. With feedback supplied,
    each excerpt must also fit a separate non-overlapping source passage.
    """
    grouped, occupied = {}, {}
    for row in excerpts:
        finding_id, quote = row["finding_id"], row["quote"].strip()
        previous = grouped.setdefault(finding_id, [])
        if not quote or any(quote in other or other in quote for other in previous):
            raise ValueError("Answer excerpts for a finding must be distinct and non-overlapping.")
        if feedback is not None:
            spans = occupied.setdefault(finding_id, [])
            free = next(((match.start(), match.end()) for match in re.finditer(re.escape(quote), feedback)
                         if all(match.end() <= start or match.start() >= end for start, end in spans)), None)
            if free is None:
                raise ValueError("Answer excerpts must quote separate source passages.")
            spans.append(free)
        previous.append(quote)
    return {key: "\n".join(quotes) for key, quotes in grouped.items()}


def _assertion_in_feedback(quote, feedback):
    """Do not launder a hypothetical by quoting only its affirmative substring."""
    if not _assertion(quote) or quote not in feedback:
        return False
    for match in re.finditer(re.escape(quote), feedback):
        left = max(feedback.rfind(mark, 0, match.start()) for mark in ("\n", ". ", "。", "! ", "? ", "！", "？"))
        stops = [position for mark in ("\n", ". ", "。", "! ", "? ", "！", "？")
                 if (position := feedback.find(mark, match.end())) >= 0]
        surrounding = feedback[left + 1:min(stops) if stops else len(feedback)]
        if _assertion(surrounding):
            prefix_line = feedback[:left].rstrip().rsplit("\n", 1)[-1] if left >= 0 else ""
            # A prior editorial instruction must not taint a later, explicit
            # declaration header on the same line: "Please improve the essay.
            # Confirmed facts:". Only the nearest sentence governs this block;
            # "Suppose:"/"For example:" still fail, including across blank lines.
            header_clause = re.split(r"[。！？]|[.!?](?:\s|$)", prefix_line)[-1]
            if not (header_clause.rstrip().endswith((":", "：")) and not _assertion(header_clause)):
                return True
    return False


def validate_editorial_context(context, review_sha256=None):
    required = {"schema_version", "feedback", "feedback_sha256", "editorial_report_sha256", "review_sha256",
                "selected_skill_ids", "interpretation", "generation_source"}
    if not isinstance(context, dict) or set(context) != required or context.get("schema_version") != CONTEXT_VERSION:
        raise ValueError("Invalid global editorial context.")
    feedback = context["feedback"]
    if not isinstance(feedback, str) or not feedback.strip() or "\0" in feedback or len(feedback.encode()) > MAX_FEEDBACK_BYTES or \
            context["feedback_sha256"] != sha256(feedback.encode()) or \
            (review_sha256 is not None and context["review_sha256"] != review_sha256) or \
            not all(isinstance(context[key], str) and re.fullmatch(r"[0-9a-f]{64}", context[key]) for key in ("review_sha256", "editorial_report_sha256")):
        raise ValueError("Global feedback does not match its source hashes.")
    result = context["interpretation"]
    ids = [row.get("finding_id") for row in result.get("decisions", [])] if isinstance(result, dict) else []
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Invalid global decision IDs.")
    validate(result, feedback_schema(ids), "feedback_plan")
    if not result["summary"].strip() or any(not row["reason"].strip() for row in result["decisions"]) or \
            context["selected_skill_ids"] != result["selected_skill_ids"] or len(set(context["selected_skill_ids"])) != len(context["selected_skill_ids"]) or \
            context["generation_source"] not in {"model", "offline_fixture"}:
        raise ValueError("Invalid selected editorial skills or interpretation.")
    for answer in result["answer_excerpts"]:
        if answer["finding_id"] not in ids or not _assertion_in_feedback(answer["quote"], feedback):
            raise ValueError("A confirmed answer must quote an explicit assertion in the author's feedback.")
    answers_from_excerpts(result["answer_excerpts"], feedback)
    return copy.deepcopy(context)


def validate_feedback_request(run_text, report, body):
    if not isinstance(body, dict) or body.get("run_sha256") != sha256(run_text.encode()):
        raise WebError(409, "原审稿版本已改变，请刷新综合报告。")
    run = json.loads(run_text)
    try:
        validate_editorial_report(run, body["run_sha256"], report)
    except (ValueError, TypeError, KeyError):
        raise WebError(409, "综合报告与审稿版本不符，请刷新后重新评价。") from None
    if body.get("editorial_report_sha256") != report["report_sha256"]:
        raise WebError(409, "综合报告已改变，请针对当前报告重新提交评价。")
    feedback = body.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip() or "\0" in feedback or len(feedback.encode()) > MAX_FEEDBACK_BYTES:
        raise WebError(400, "请填写整体评价（最多 32 KB），说明你希望保留和调整的内容。")
    if run["status"] != "complete":
        raise WebError(409, "审稿尚未完整完成，请先处理诊断或重试裁决。")
    from .revision import prepare_revision_plan
    try:
        checked = prepare_revision_plan(run, body["run_sha256"], confirmed_facts=body.get("confirmed_facts", []),
            essay_type=body.get("essay_type", "sop"), writing_sample=body.get("writing_sample", ""),
            word_limit=body.get("word_limit"), glossary=body.get("glossary"))
    except (ValueError, TypeError, KeyError):
        raise WebError(400, "整体评价的类型、字数或补充素材格式不正确，请核对后重试。") from None
    if checked["unconfirmed_fact_ids"]:
        raise WebError(400, "补充素材需要作者先确认；未确认的素材不会发送给模型。")
    return {"feedback": feedback, **{key: copy.deepcopy(checked[key]) for key in
            ("essay_type", "word_limit", "writing_sample", "glossary", "confirmed_facts")}}


def demo_feedback(manifest):
    facts = [row["text"] for row in manifest["plan_input"].get("confirmed_facts", []) if row.get("confirmed")]
    answers = list(manifest["plan_input"].get("answers", {}).values())
    return "请根据综合建议优化整篇文章，保留真实经历、技术含义和我的语气。以下是我确认的信息：\n" + "\n".join(facts + answers)


def _offline_interpretation(run, feedback, demo_manifest):
    findings = findings_for_report(run)
    matched = demo_manifest is not None and feedback == demo_feedback(demo_manifest)
    if matched:
        options = demo_manifest["plan_input"]
        return {"summary": "离线固定样例：按整体评价采用示例建议，并逐字引用已确认的回答。", "decisions": copy.deepcopy(options["decisions"]),
                "answer_excerpts": [{"finding_id": key, "quote": value} for key, value in options.get("answers", {}).items()],
                "selected_skill_ids": list(SKILL_IDS), "unresolved_questions": []}
    return {"summary": "离线模式不解析任意评价，当前保留原稿。请加载完整示例或切换在线模式。",
            "decisions": [{"finding_id": row["finding_id"], "decision": "defer", "reason": "任意评价需要在线模型解析；离线模式保留原稿。"} for row in findings],
            "answer_excerpts": [], "selected_skill_ids": list(SKILL_IDS),
            "unresolved_questions": ["离线演示仅支持未修改的示例评价；实际评价需要配置在线模型。"]}


def prepare_feedback_plan(run, run_sha256, report, body, provider=None, *, knowledge_context=None, demo_manifest=None):
    from .revision import prepare_revision_plan
    validate_editorial_report(run, run_sha256, report)
    feedback = body["feedback"]
    findings = findings_for_report(run)
    payload = {"original": run["snapshot"], "editorial_report": report, "findings": findings,
               "author_feedback": feedback, "confirmed_facts": body.get("confirmed_facts", []),
               "essay_type": body.get("essay_type", "sop"), "local_skill_catalog": skill_catalog()}
    output = provider.task("feedback_plan", payload, feedback_schema([row["finding_id"] for row in findings])) if provider is not None else \
        _offline_interpretation(run, feedback, demo_manifest)
    validate(output, feedback_schema([row["finding_id"] for row in findings]), "feedback_plan")
    ids = [row["finding_id"] for row in output["decisions"]]
    if sorted(ids) != sorted(row["finding_id"] for row in findings):
        raise ValueError("Feedback interpretation must decide every original finding exactly once.")
    context = validate_editorial_context({"schema_version": CONTEXT_VERSION, "feedback": feedback,
        "feedback_sha256": sha256(feedback.encode()), "editorial_report_sha256": report["report_sha256"], "review_sha256": run_sha256,
        "selected_skill_ids": output["selected_skill_ids"], "interpretation": output, "generation_source": "model" if provider else "offline_fixture"}, run_sha256)
    return prepare_revision_plan(run, run_sha256, decisions=output["decisions"], confirmed_facts=body.get("confirmed_facts", []),
        answers=answers_from_excerpts(output["answer_excerpts"], feedback), essay_type=body.get("essay_type", "sop"),
        writing_sample=body.get("writing_sample", ""), word_limit=body.get("word_limit"), glossary=body.get("glossary"),
        knowledge_context=knowledge_context, editorial_context=context)


def feedback_result_fields(record):
    context = record.get("plan", {}).get("editorial_context")
    if not context:
        return {}
    return {"feedback": context["feedback"], "feedback_analysis": copy.deepcopy(context["interpretation"]),
            "selected_skill_ids": list(context["selected_skill_ids"])}
