"""Version-bound, author-directed full-essay revision with a final read-only check.

All APIs are pure with respect to persistent storage. Hosts publish artifacts and
acceptance receipts separately; the original review and manuscript never change.
"""

import copy
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from .contracts import validate
from .review import (NUMBERS, fingerprint, paragraph_spans, sha256, unified_diff,
                     knowledge_evidence, merge_knowledge_evidence, render_knowledge, validate_knowledge_context)
from .revision_schema import CHECK_NAMES, REVISION_SCHEMAS
from .workflow import revalidate_reviewers, verify_run
from .technical import (check_technical_accuracy, extract_technical_context,
                        normalize_glossary, validate_writing_technical_flags)

REVISION_VERSION = "full-revision-4"
WRITING_STAGES = ("sop_revise", "polish", "humanize")
ALL_STAGES = (*WRITING_STAGES, "verify_revision")
MAX_ESSAY_BYTES = 128 * 1024
DECISIONS = {"accept", "reject", "defer"}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _manifest():
    from .editorial_skills import editorial_manifest
    return editorial_manifest()


def _seal(value, field):
    value.pop(field, None)
    value[field] = fingerprint(value)
    return value


def _check_seal(value, field):
    if value.get(field) != fingerprint({k: v for k, v in value.items() if k != field}):
        raise ValueError("Revision record or plan checksum differs; prepare a new plan.")


def _finding_rows(run):
    rows = []
    seen = set()
    for group in run["groups"]:
        for finding in group["findings"]:
            if finding["id"] in seen:
                raise ValueError("Review finding IDs must be unique.")
            seen.add(finding["id"])
            rows.append({**copy.deepcopy(finding), "finding_id": finding["id"], "group_id": group["id"]})
    return rows


def _word_limit(requirements, supplied):
    if supplied is not None and (isinstance(supplied, bool) or not isinstance(supplied, int) or not 1 <= supplied <= 20000):
        raise ValueError("word_limit must be an integer between 1 and 20000.")
    matches = re.findall(r"(?:maximum|max\.?|up to|no more than|limit(?:ed)?(?:\s+to)?)\s*[:=]?\s*(\d{1,5})\s*(?:English\s+)?words?", requirements, re.I)
    matches += re.findall(r"(\d{1,5})\s*[- ]word\s+(?:limit|maximum)", requirements, re.I)
    parsed = min((int(n) for n in matches if int(n) > 0), default=None)
    return min(supplied, parsed) if supplied and parsed else supplied or parsed


def _facts(run, confirmed_facts, answers):
    facts, unconfirmed = [], []
    ids = set()
    evidence = run["snapshot"]["evidence"]
    if not isinstance(confirmed_facts, list):
        raise ValueError("confirmed_facts must be a list.")
    for index, raw in enumerate(confirmed_facts):
        if not isinstance(raw, dict) or set(raw) - {"id", "text", "confirmed", "kind", "evidence_ids", "citations"}:
            raise ValueError("Invalid confirmed fact fields.")
        fact_id = raw.get("id", f"FACT-{index + 1:03d}")
        if not isinstance(fact_id, str) or not fact_id.strip() or fact_id in ids or fact_id.startswith("ANSWER-"):
            raise ValueError("Confirmed fact IDs must be nonempty and unique.")
        ids.add(fact_id)
        if raw.get("confirmed") is not True:
            unconfirmed.append(fact_id)
            continue  # Do not retain or send unconfirmed prose to any writing pass.
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip() or "\0" in text or len(text.encode()) > MAX_ESSAY_BYTES:
            raise ValueError("Confirmed facts need nonempty plain text within 128 KB.")
        source_ids = raw.get("evidence_ids", [])
        citations = raw.get("citations", [])
        if not isinstance(source_ids, list) or any(not isinstance(i, str) for i in source_ids) or set(source_ids) - evidence.keys():
            raise ValueError("Confirmed facts reference an unknown evidence ID.")
        if not isinstance(citations, list):
            raise ValueError("Fact citations must be a list.")
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"source_id", "quote"} or \
                    citation["source_id"] not in evidence or not isinstance(citation["quote"], str) or \
                    not citation["quote"].strip() or citation["quote"] not in evidence[citation["source_id"]]:
                raise ValueError("Confirmed fact evidence excerpts must be exact supplied source quotes.")
        kind = raw.get("kind", "author_confirmation")
        if kind not in {"author_confirmation", "personal_contribution", "team_outcome", "number", "program_info", "goal", "motivation"}:
            raise ValueError("Unknown confirmed fact kind.")
        facts.append({"id": fact_id, "text": text.strip(), "confirmed": True, "kind": kind,
                      "evidence_ids": source_ids, "citations": citations})
    for finding_id, answer in answers.items():
        fact_id = "ANSWER-" + finding_id
        if fact_id in ids:
            raise ValueError("A confirmed fact ID conflicts with an applicant answer.")
        facts.append({"id": fact_id, "text": answer, "confirmed": True, "kind": "author_confirmation", "evidence_ids": [], "citations": []})
    return facts, unconfirmed


def _revision_snapshot(run, knowledge_context):
    """Replace historical retrieved excerpts while retaining author evidence."""
    snapshot = copy.deepcopy(run["snapshot"])
    previous_ids = knowledge_evidence(snapshot.get("knowledge_context"))
    supplied = {key: value for key, value in snapshot["evidence"].items() if key not in previous_ids}
    snapshot["evidence"] = merge_knowledge_evidence(supplied, knowledge_context)
    if knowledge_context is None:
        snapshot.pop("knowledge_context", None)
    else:
        snapshot["knowledge_context"] = copy.deepcopy(knowledge_context)
    return snapshot


def _knowledge_changes(previous, current):
    if not previous or not previous["passages"]:
        return []
    before = {source["name"]: source for source in previous["sources"]}
    after = {source["name"]: source for source in current["sources"]} if current else {}
    warnings = []
    for name in sorted({passage["repository"] for passage in previous["passages"]}):
        old, new = before[name], after.get(name)
        if new is None or new.get("status") != "ready" or \
                (old.get("commit_sha"), old.get("snapshot_hash")) != (new.get("commit_sha"), new.get("snapshot_hash")):
            warnings.append(f"Repository {name} differs from the snapshot used for the review. Historical repository excerpts were replaced; recheck earlier findings against the current selected passages.")
    return warnings


def prepare_revision_plan(run, review_sha256, decisions=None, confirmed_facts=None, answers=None,
                          essay_type="sop", writing_sample="", word_limit=None, glossary=None, knowledge_context=None):
    verify_run(run)
    if not isinstance(review_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", review_sha256):
        raise ValueError("Plan requires the SHA-256 of the reviewed run.json bytes.")
    if essay_type not in {"sop", "personal_statement", "short_answer"}:
        raise ValueError("essay_type must be sop, personal_statement or short_answer.")
    if not isinstance(writing_sample, str) or "\0" in writing_sample or len(writing_sample.encode()) > MAX_ESSAY_BYTES:
        raise ValueError("Voice sample must be plain text within 128 KB.")
    rows = _finding_rows(run)
    by_id = {row["finding_id"]: row for row in rows}
    if decisions is None:
        decisions = [{"finding_id": key, "decision": "defer", "reason": "等待作者逐项决定"} for key in by_id]
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("decisions must be a list of finding decisions.")
    decided = {}
    for decision in decisions:
        if set(decision) - {"finding_id", "decision", "reason"} or decision.get("finding_id") not in by_id or \
                decision["finding_id"] in decided or decision.get("decision") not in DECISIONS or \
                not isinstance(decision.get("reason", ""), str):
            raise ValueError("Every known finding requires one accept/reject/defer decision.")
        if not decision.get("reason", "").strip():
            raise ValueError("Each revision decision requires an author reason.")
        decided[decision["finding_id"]] = {"decision": decision["decision"], "reason": decision["reason"].strip()}
    if set(decided) != set(by_id):
        raise ValueError("Missing revision decisions; decide every finding exactly once.")
    answers = answers or {}
    if not isinstance(answers, dict) or set(answers) - by_id.keys() or any(not isinstance(v, str) or not v.strip() or "\0" in v or len(v.encode()) > MAX_ESSAY_BYTES for v in answers.values()):
        raise ValueError("Applicant answers must be nonempty confirmed text keyed by known finding IDs.")
    answers = {k: v.strip() for k, v in answers.items()}
    knowledge_context = validate_knowledge_context(knowledge_context if knowledge_context is not None else
                                                   run["snapshot"].get("knowledge_context"))
    revision_snapshot = _revision_snapshot(run, knowledge_context)
    facts, unconfirmed = _facts({**run, "snapshot": revision_snapshot}, confirmed_facts or [], answers)
    glossary = normalize_glossary(glossary)
    technical_context = extract_technical_context(revision_snapshot, glossary, facts)
    blockers, pending, protected = [], [], []
    complete = run["status"] == "complete" and all(v["status"] == "complete" for v in run["reviewers"].values())
    if not complete:
        blockers.append("审稿或裁决未完整完成，不能依据部分审稿接受最终全文。")
    else:
        revalidate_reviewers(run)
    for row in rows:
        row.update(decided[row["finding_id"]])
        needs_answer = row["kind"] == "question" or row["action"] == "verify"
        row["question"] = row["rationale"] if needs_answer else None
        row["answer"] = answers.get(row["finding_id"])
        if row["decision"] == "accept" and needs_answer and not row["answer"]:
            pending.append(f"{row['finding_id']}: 请作者回答：{row['rationale']}")
        if row["decision"] == "defer" and (row["severity"] != "low" or needs_answer):
            pending.append(f"{row['finding_id']}: 实质问题尚待作者决定。")
        if (row["decision"] == "accept" and row["action"] == "preserve") or \
                (row["decision"] in {"reject", "defer"} and row["action"] != "preserve"):
            protected.append({"finding_id": row["finding_id"], "quote": row["quote"], "paragraph_id": row["paragraph_id"],
                              "reason": row["decision"]})
    accepted_edits = [row for row in rows if row["decision"] == "accept" and row["action"] != "preserve"]
    for accepted in accepted_edits:
        for guard in protected:
            other = by_id[guard["finding_id"]]
            if max(accepted["start"], other["start"]) < min(accepted["end"], other["end"]):
                blockers.append(f"{accepted['finding_id']} 与 {other['finding_id']} 的采纳/保护要求重叠，请统一决定。")
    for index, left in enumerate(accepted_edits):
        for right in accepted_edits[index + 1:]:
            if max(left["start"], right["start"]) < min(left["end"], right["end"]) and \
                    left.get("replacement") and right.get("replacement") and left["replacement"] != right["replacement"]:
                blockers.append(f"{left['finding_id']} 与 {right['finding_id']} 的获准替换冲突。")
    pending.extend(f"请作者确认素材 {fact_id}，未确认正文不会送入模型。" for fact_id in unconfirmed)
    pending.extend(f"{fact['id']}: 项目信息需要可复读的官方证据片段。" for fact in facts if fact["kind"] == "program_info" and not fact["citations"])
    plan = {"schema_version": REVISION_VERSION, "review_sha256": review_sha256,
            "review_content_sha256": fingerprint(run), "draft_sha256": run["draft_sha256"],
            "input_sha256": run["input_sha256"], "evidence_sha256": copy.deepcopy(run["evidence_sha256"]),
            "essay_type": essay_type, "writing_sample": writing_sample,
            "glossary": glossary, "technical_context": technical_context,
            "knowledge_context": knowledge_context,
            "knowledge_warnings": _knowledge_changes(run["snapshot"].get("knowledge_context"), knowledge_context),
            "revision_evidence_sha256": {key: sha256(value.encode()) for key, value in revision_snapshot["evidence"].items()},
            "word_limit": _word_limit(run["snapshot"]["requirements"], word_limit),
            "word_count_method": "Unicode whitespace split; inferred English word limits only",
            "findings": rows, "confirmed_facts": facts, "answers": answers,
            "unconfirmed_fact_ids": unconfirmed, "protected_quotes": protected,
            "blockers": list(dict.fromkeys(blockers)), "unresolved_questions": list(dict.fromkeys(pending)),
            "skills": _manifest()}
    return _seal(plan, "plan_sha256")


def validate_revision_plan(run, plan):
    verify_run(run)
    _check_seal(plan, "plan_sha256")
    if plan.get("schema_version") != REVISION_VERSION or plan.get("review_content_sha256") != fingerprint(run) or \
            plan.get("draft_sha256") != run["draft_sha256"] or plan.get("input_sha256") != run["input_sha256"] or \
            plan.get("evidence_sha256") != run["evidence_sha256"]:
        raise ValueError("Revision plan is stale or bound to different reviewed inputs.")
    # Rebuild from author-controlled decisions, never trust edited source quotes,
    # severities, evidence, blocker flags or skill hashes in a saved plan.
    facts = [f for f in plan["confirmed_facts"] if not f["id"].startswith("ANSWER-")]
    facts += [{"id": fact_id, "confirmed": False} for fact_id in plan.get("unconfirmed_fact_ids", [])]
    expected = prepare_revision_plan(run, plan["review_sha256"],
        [{k: row[k] for k in ("finding_id", "decision", "reason")} for row in plan["findings"]],
        facts, plan["answers"], plan["essay_type"], plan["writing_sample"], plan["word_limit"], plan["glossary"],
        plan["knowledge_context"])
    if expected != plan:
        raise ValueError("Revision plan source fields or policies differ; prepare a fresh plan.")
    return True


def _numbers(text):
    units = {"percent": "%", "per cent": "%", "percentage": "%", "kilograms": "kg", "kilogram": "kg",
             "milliseconds": "ms", "millisecond": "ms", "seconds": "s", "second": "s", "minutes": "min",
             "minute": "min", "hours": "h", "hour": "h", "days": "day", "years": "year",
             "percentage points": "pp", "percentage point": "pp"}
    result = set()
    for match in NUMBERS.finditer(text):
        value = match.group().replace(",", "")
        if not value.endswith(("%", "+")):
            suffix = re.match(r"[\s-]*(percentage points?|per cent|percent|percentage|kilograms?|kg|milliseconds?|ms|seconds?|s|minutes?|min|hours?|h|days?|years?|million|billion|users|participants|students|trials|experiments)\b", text[match.end():], re.I)
            if suffix:
                unit = suffix.group(1).lower()
                if unit in {"million", "billion"}:
                    value = format(Decimal(value) * (1000000 if unit == "million" else 1000000000), "f")
                    if "." in value:
                        value = value.rstrip("0").rstrip(".")
                else:
                    value += units.get(unit, unit)
        result.add(value)
    return result


def _strong_attribution(text):
    return set(re.findall(r"\b(?:independently|single-handedly|solely|entire|sole)\b|\bI\s+(?:personally\s+)?(?:led|founded|owned|directed)\b", text, re.I))


def _entities(text):
    # Conservative explicit proper-name phrases; semantic/entity completeness is
    # separately checked by the read-only verifier and remains human-reviewable.
    return set(re.findall(r"\b(?:Professor|Prof\.|Dr\.|University of|Institute of|Laboratory of)\s+[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,4}|\b[A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,3}\s+(?:University|Institute|Laboratory)\b", text))


def _writing_technical_issues(plan, stages, output):
    flags = {flag["claim_id"]: flag for stage in stages if stage["stage"] in WRITING_STAGES
             for flag in stage["output"]["technical_flags"]}
    flags.update({flag["claim_id"]: flag for flag in output["technical_flags"]})
    return (check_technical_accuracy(plan["technical_context"], output["essay"], used_fact_ids=output["used_fact_ids"])["issues"] +
            validate_writing_technical_flags(plan["technical_context"], output["essay"], output["technical_flags"]) +
            validate_writing_technical_flags(plan["technical_context"], output["essay"], list(flags.values())))


def _technical_result(plan, stages, candidate, rejected_stage=None):
    verified = next((stage["output"] for stage in stages if stage["stage"] == "verify_revision"), None)
    used_facts = {fact_id for stage in stages if stage["stage"] in WRITING_STAGES
                  for fact_id in stage["output"]["used_fact_ids"]}
    report = check_technical_accuracy(plan["technical_context"], candidate,
                                     verified["technical_checks"] if verified else None, used_fact_ids=used_facts)
    report["complete"] = verified is not None
    flags = [dict(flag, stage=stage["stage"]) for stage in stages if stage["stage"] in WRITING_STAGES
             for flag in stage["output"]["technical_flags"]]
    report["writing_flags"] = flags
    active_flags = {flag["claim_id"]: {key: flag[key] for key in ("claim_id", "reason")} for flag in flags}
    report["issues"].extend(validate_writing_technical_flags(plan["technical_context"], candidate,
                            list(active_flags.values())))
    if rejected_stage is not None:
        report["rejected_stage"] = rejected_stage["stage"]
        report["issues"].extend(_writing_technical_issues(plan, stages, rejected_stage["output"]))
    report["passed"] = report["passed"] and report["complete"] and not report["issues"]
    return report


def _mechanical(run, plan, stages, rejected_stage=None):
    blockers = list(plan["blockers"]) + list(plan["unresolved_questions"])
    texts = {stage["stage"]: stage["output"]["essay"] for stage in stages if stage["stage"] in WRITING_STAGES and stage.get("output")}
    original = run["snapshot"]["draft"]
    changes = {}
    accepted = {f["finding_id"] for f in plan["findings"] if f["decision"] == "accept"}
    fact_ids = {f["id"] for f in plan["confirmed_facts"]}
    for stage in stages:
        name, output = stage["stage"], stage.get("output")
        if name not in WRITING_STAGES or not output:
            continue
        essay = output["essay"]
        used_fact_ids = set(output["used_fact_ids"])
        # An available confirmation licenses a new detail only when this pass
        # reports its source ID; another pass's declaration does not cover it.
        allowed_facts = original + "\n" + "\n".join(
            fact["text"] for fact in plan["confirmed_facts"] if fact["id"] in used_fact_ids)
        numbers = _numbers(allowed_facts)
        attribution = {s.lower() for s in _strong_attribution(allowed_facts)}
        entities = _entities(allowed_facts + "\n" + run["snapshot"]["requirements"])
        if not essay.strip() or "\0" in essay or len(essay.encode()) > MAX_ESSAY_BYTES:
            blockers.append(f"{name}: 全文为空、含非法字符或超过 128 KB。")
        if len(set(output["addressed_finding_ids"])) != len(output["addressed_finding_ids"]) or set(output["addressed_finding_ids"]) - accepted:
            blockers.append(f"{name}: 引用了未采纳或未知的审稿发现。")
        if used_fact_ids - fact_ids:
            blockers.append(f"{name}: 引用了未确认或未知的作者事实。")
        for guard in plan["protected_quotes"]:
            if guard["quote"] not in essay:
                blockers.append(f"{name}: {guard['finding_id']} 的拒绝/保留原句被改动。")
        new_numbers = sorted(_numbers(essay) - numbers)
        new_entities = sorted(_entities(essay) - entities)
        scope = sorted({s.lower() for s in _strong_attribution(essay)} - attribution)
        if new_numbers:
            blockers.append(f"{name}: 新增未确认数字：{', '.join(new_numbers)}。")
        if new_entities:
            blockers.append(f"{name}: 新增未确认人物/机构名称，请核对来源。")
        if scope:
            blockers.append(f"{name}: 个人贡献范围可能被扩大，请作者确认。")
        if output["unresolved_questions"]:
            blockers.extend(f"{name}: {q}" for q in output["unresolved_questions"])
        changes[name] = {"added_numbers": sorted(_numbers(essay) - _numbers(original)),
                         "removed_numbers": sorted(_numbers(original) - _numbers(essay)),
                         "added_named_phrases": new_entities, "attribution_expansion_terms": scope}
    final = texts.get("humanize", "")
    if final:
        missing = accepted - set(next(s["output"]["addressed_finding_ids"] for s in stages if s["stage"] == "humanize"))
        blockers.extend(f"{fid}: 最终稿没有可追溯的采纳记录。" for fid in sorted(missing))
        if plan["word_limit"] and len(final.split()) > plan["word_limit"]:
            blockers.append(f"超过题目字数上限：{len(final.split())}/{plan['word_limit']}。")
        if re.search(r"\[\s*(?:待补|TODO|TBD|missing|insert)\b|\[待补", final, re.I):
            blockers.append("最终候选仍含待补占位内容。")
    else:
        blockers.append("Humanizer 最后写作阶段未完成。")
    candidate = next((stage["output"]["essay"] for stage in reversed(stages)
                      if stage["stage"] in WRITING_STAGES), original)
    technical = _technical_result(plan, stages, candidate, rejected_stage)
    blockers.extend("技术核验：" + issue["reason"] for issue in technical["issues"])
    if not technical["complete"]:
        blockers.append("Humanizer 后的只读技术准确性核验尚未完成。")
    return {"blockers": list(dict.fromkeys(blockers)), "word_count": len(final.split()),
            "word_limit": plan["word_limit"], "factual_changes": changes,
            "technical_accuracy": technical,
            "scope": "Mechanical checks plus independent model/fixture verification; not proof of semantic truth."}


def _verify_output(run, plan, stages, result):
    validation = _mechanical(run, plan, stages)
    blockers = validation["blockers"]
    final = next((s["output"]["essay"] for s in stages if s["stage"] == "humanize"), "")
    checks = result["checks"]
    if len(checks) != len(CHECK_NAMES) or {c["check"] for c in checks} != set(CHECK_NAMES):
        blockers.append("只读核验没有完整覆盖所有必需检查。")
    for check in checks:
        if not check["passed"] or not check["rationale"].strip():
            blockers.append(f"核验未通过：{check['check']}。")
    expected = {r["finding_id"]: r for r in plan["findings"]}
    finding_checks = result["finding_checks"]
    if len(finding_checks) != len(expected) or {c["finding_id"] for c in finding_checks} != set(expected):
        blockers.append("只读核验未逐项覆盖所有审稿发现。")
    for check in finding_checks:
        finding = expected.get(check["finding_id"])
        if finding is None:
            continue
        if not check["rationale"].strip() or not check["quote"].strip() or check["quote"] not in final:
            blockers.append(f"{finding['finding_id']}: 核验引用不属于最终全文。")
        if finding["decision"] == "accept":
            acceptable = {"preserved"} if finding["action"] == "preserve" else {"addressed"}
        else:
            acceptable = {"preserved", "unresolved"}
        if check["status"] not in acceptable:
            blockers.append(f"{finding['finding_id']}: 核验结果不符合作者决定。")
        if check["status"] == "unresolved" and (finding["decision"] == "accept" or finding["severity"] != "low"):
            blockers.append(f"{finding['finding_id']}: 实质问题尚未解决。")
    source_lookup = {"original": {p["id"]: p["text"] for p in run["snapshot"]["paragraphs"]},
                     "confirmed_fact": {f["id"]: f["text"] for f in plan["confirmed_facts"]},
                     "evidence": _revision_snapshot(run, plan["knowledge_context"])["evidence"]}
    good_claims = []
    for claim in result["claim_changes"]:
        source = source_lookup[claim["source_type"]].get(claim["source_id"], "")
        removal = claim["final_quote"] is None
        original_quote = claim["original_quote"]
        authorized_removal = removal and original_quote and any(
            # A local grammar finding cannot authorize discarding other claims
            # in its paragraph. A deletion must stay inside an accepted span.
            f["decision"] == "accept" and f["action"] != "preserve" and original_quote in f["quote"]
            for f in plan["findings"])
        if claim["status"] != "supported" or \
                (not removal and (not claim["final_quote"].strip() or claim["final_quote"] not in final)) or \
                (removal and (not authorized_removal or claim["source_type"] == "original")) or \
                not claim["source_quote"].strip() or claim["source_quote"] not in source or \
                (original_quote is not None and (not original_quote.strip() or original_quote not in run["snapshot"]["draft"])):
            blockers.append("事实变化存在未支持、无法复读或不确定的来源引用。")
        else:
            good_claims.append(claim)
    used_facts = {fact_id for stage in stages if stage["stage"] in WRITING_STAGES for fact_id in stage["output"]["used_fact_ids"]}
    traced_facts = {c["source_id"] for c in good_claims if c["source_type"] == "confirmed_fact"}
    blockers.extend(f"{fact_id}: 已使用的确认事实缺少核验来源记录。" for fact_id in sorted(used_facts - traced_facts))
    blockers.extend(f"{fact_id}: 核验引用的确认事实未在写作阶段声明使用。" for fact_id in sorted(traced_facts - used_facts))
    for paragraph in paragraph_spans(final).values():
        if not any(c["final_quote"] == paragraph[1] for c in good_claims):
            blockers.append("最终稿有整段内容缺少完整、可复读的事实来源核验。")
    for paragraph in run["snapshot"]["paragraphs"]:
        if not any(c["original_quote"] == paragraph["text"] for c in good_claims):
            blockers.append(f"{paragraph['id']}: 原稿段落的事实变化未完整核对。")
    removed_numbers = _numbers(run["snapshot"]["draft"]) - _numbers(final)
    explained_removals = {n for c in good_claims if c["final_quote"] is None for n in _numbers(c["original_quote"] or "")}
    blockers.extend(f"删除的原稿数字 {n} 缺少明确的事实移除记录。" for n in sorted(removed_numbers - explained_removals))
    blockers.extend("核验待补：" + q for q in result["unresolved_questions"])
    validation.update({"blockers": list(dict.fromkeys(blockers)), "passed": not blockers,
                       "semantic_checks": copy.deepcopy(checks), "finding_checks": copy.deepcopy(finding_checks),
                       "claim_changes": copy.deepcopy(result["claim_changes"]),
                       "unresolved_questions": list(dict.fromkeys(plan["unresolved_questions"] + result["unresolved_questions"]))})
    return validation


def _stage_call(provider, stage, payload):
    from .editorial_skills import load_editorial_skill
    skill = load_editorial_skill(stage)
    frozen = json.loads(json.dumps(payload, ensure_ascii=False))
    output = provider.task(stage, frozen, copy.deepcopy(REVISION_SCHEMAS[stage]))
    validate(output, REVISION_SCHEMAS[stage], stage)
    label = getattr(provider, "label_for", lambda _: provider.label)(stage)
    return {"stage": stage, "provider": label, "prompt_version": skill["version"], "prompt_sha256": skill["sha256"],
            "request_sha256": fingerprint(payload), "response_sha256": fingerprint(output), "output": copy.deepcopy(output),
            "metrics": copy.deepcopy(getattr(provider, "call_records", {}).get(stage, {}))}


def _payload(run, plan, stages, stage, current):
    payload = {"original": _revision_snapshot(run, plan["knowledge_context"]), "revision_plan": copy.deepcopy(plan),
               "accepted_findings": [copy.deepcopy(row) for row in plan["findings"] if row["decision"] == "accept"],
               "confirmed_facts": copy.deepcopy(plan["confirmed_facts"]),
               "requirements": run["snapshot"]["requirements"], "preferences": run["snapshot"]["preferences"],
               "voice_reference": plan["writing_sample"] or run["snapshot"]["draft"], "essay_type": plan["essay_type"],
               "technical_context": copy.deepcopy(plan["technical_context"]),
               "knowledge_context": copy.deepcopy(plan["knowledge_context"]),
               "current_essay": current}
    if stage == "verify_revision":
        payload["versions"] = {"original": run["snapshot"]["draft"],
                               **{item["stage"]: item["output"]["essay"] for item in stages}}
        payload["mechanical_checks"] = _mechanical(run, plan, stages)
        # This request is about to perform the read-only check. Its absence
        # before this call is pending work, not an adverse finding about text.
        payload["mechanical_checks"]["blockers"] = [reason for reason in payload["mechanical_checks"]["blockers"]
            if reason != "Humanizer 后的只读技术准确性核验尚未完成。"]
        payload["mechanical_checks"]["technical_accuracy"] = {
            **check_technical_accuracy(plan["technical_context"], current, used_fact_ids={
                fact_id for item in stages if item["stage"] in WRITING_STAGES for fact_id in item["output"]["used_fact_ids"]}),
            "model_verification_pending": True}
    return payload


def _trace(plan, stages):
    final_checks = next((s["output"]["finding_checks"] for s in stages if s["stage"] == "verify_revision"), [])
    by_id = {c["finding_id"]: c for c in final_checks}
    result = []
    for row in plan["findings"]:
        addressed = [item["stage"] for item in stages if item["stage"] in WRITING_STAGES and row["finding_id"] in item["output"]["addressed_finding_ids"]]
        check = by_id.get(row["finding_id"])
        result.append({"finding_id": row["finding_id"], "group_id": row["group_id"], "decision": row["decision"],
                       "reason": row["reason"], "original_quote": row["quote"], "original_paragraph_id": row["paragraph_id"],
                       "addressed_in_stages": addressed, "final_quote": check["quote"] if check else None,
                       "verification_status": check["status"] if check else "unverified",
                       "verification_rationale": check["rationale"] if check else None,
                       "status": ("addressed" if "humanize" in addressed else "unresolved") if row["decision"] == "accept" else row["decision"]})
    return result


def revise_document(run, plan, provider):
    validate_revision_plan(run, plan)
    record = {"schema_version": REVISION_VERSION, "source_review": copy.deepcopy(run), "plan": copy.deepcopy(plan),
              "status": "blocked", "candidate": run["snapshot"]["draft"], "stages": [], "trace": [],
              "validation": {"passed": False, "blockers": list(plan["blockers"])}, "errors": [],
              "rejected_stage": None}
    if not plan["blockers"]:
        current = run["snapshot"]["draft"]
        for stage in ALL_STAGES:
            payload = _payload(run, plan, record["stages"], stage, current)
            try:
                result = _stage_call(provider, stage, payload)
                if stage in WRITING_STAGES and _writing_technical_issues(plan, record["stages"], result["output"]):
                    # Keep the latest safe wording; retain the rejected proposal
                    # separately for source-backed, read-only inspection.
                    record["rejected_stage"] = result
                    record["validation"] = {**_mechanical(run, plan, record["stages"], result), "passed": False}
                    break
                record["stages"].append(result)
                if stage in WRITING_STAGES:
                    current = result["output"]["essay"]
                    record["candidate"] = current
                else:
                    record["validation"] = _verify_output(run, plan, record["stages"], result["output"])
            except Exception as error:
                record["errors"].append({"stage": stage, "error_type": type(error).__name__})
                record["validation"] = {**_mechanical(run, plan, record["stages"]), "passed": False}
                record["validation"]["blockers"].append(f"{stage}: 模型/结构校验失败（{type(error).__name__}），未接受为最终稿。")
                break
        if record["validation"]["passed"]:
            record["status"] = "ready"
        elif record["errors"]:
            record["status"] = "failed"
    record["candidate_sha256"] = sha256(record["candidate"].encode())
    record["technical_accuracy"] = _technical_result(plan, record["stages"], record["candidate"], record["rejected_stage"])
    record["trace"] = _trace(plan, record["stages"])
    record["full_diff"] = unified_diff(run["snapshot"]["draft"], record["candidate"])
    record["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return _seal(record, "record_sha256")


def verify_revision(record):
    from .editorial_skills import load_editorial_skill
    _check_seal(record, "record_sha256")
    if record.get("schema_version") != REVISION_VERSION:
        raise ValueError("Unsupported revision version.")
    run, plan = record["source_review"], record["plan"]
    validate_revision_plan(run, plan)
    stages = record["stages"]
    names = [item["stage"] for item in stages]
    if names != list(ALL_STAGES[:len(names)]):
        raise ValueError("Revision stages must remain in order; Humanizer is the last writing stage.")
    current = run["snapshot"]["draft"]
    for index, item in enumerate(stages):
        validate(item["output"], REVISION_SCHEMAS[item["stage"]], item["stage"])
        skill = load_editorial_skill(item["stage"])
        if item["response_sha256"] != fingerprint(item["output"]) or \
                item["request_sha256"] != fingerprint(_payload(run, plan, stages[:index], item["stage"], current)) or \
                item["prompt_sha256"] != skill["sha256"] or item["prompt_version"] != skill["version"]:
            raise ValueError("Stored revision stage output differs.")
        if item["stage"] in WRITING_STAGES:
            if _writing_technical_issues(plan, stages[:index], item["output"]):
                raise ValueError("A writing stage changed protected technical meaning.")
            current = item["output"]["essay"]
    rejected = record["rejected_stage"]
    if rejected is not None:
        stage = rejected["stage"]
        skill = load_editorial_skill(stage)
        if stage not in WRITING_STAGES or len(stages) >= len(WRITING_STAGES) or stage != ALL_STAGES[len(stages)]:
            raise ValueError("Rejected technical proposal is out of order.")
        validate(rejected["output"], REVISION_SCHEMAS[stage], stage)
        if rejected["response_sha256"] != fingerprint(rejected["output"]) or \
                rejected["request_sha256"] != fingerprint(_payload(run, plan, stages, stage, current)) or \
                rejected["prompt_sha256"] != skill["sha256"] or rejected["prompt_version"] != skill["version"] or \
                not _writing_technical_issues(plan, stages, rejected["output"]):
            raise ValueError("Rejected technical proposal or its source trace differs.")
    expected = next((item["output"]["essay"] for item in reversed(stages) if item["stage"] in WRITING_STAGES), run["snapshot"]["draft"])
    if record["candidate"] != expected or record["candidate_sha256"] != sha256(expected.encode()) or \
            record["full_diff"] != unified_diff(run["snapshot"]["draft"], expected):
        raise ValueError("Revision candidate or diff differs from saved writing stages.")
    if plan["blockers"]:
        validation = {"passed": False, "blockers": list(plan["blockers"])}
    elif names == list(ALL_STAGES):
        validation = _verify_output(run, plan, stages, stages[-1]["output"])
    else:
        validation = {**_mechanical(run, plan, stages, rejected), "passed": False}
        for error in record["errors"]:
            validation["blockers"].append(f"{error['stage']}: 模型/结构校验失败（{error['error_type']}），未接受为最终稿。")
    expected_status = "ready" if validation["passed"] else "failed" if record["errors"] else "blocked"
    if record["validation"] != validation or record["trace"] != _trace(plan, stages) or record["status"] != expected_status:
        raise ValueError("Revision validation, trace or ready status differs from revalidated outputs.")
    if record["technical_accuracy"] != _technical_result(plan, stages, record["candidate"], rejected):
        raise ValueError("Technical accuracy report differs from its sources and stage outputs.")
    return True


def finalize_revision(record, candidate_sha256, expected_plan_sha256=None):
    verify_revision(record)
    if record["status"] != "ready" or not record["validation"]["passed"]:
        raise ValueError("Revision has blockers; only a candidate is available, not an acceptable final essay.")
    if candidate_sha256 != record["candidate_sha256"] or \
            (expected_plan_sha256 is not None and expected_plan_sha256 != record["plan"]["plan_sha256"]):
        raise ValueError("Acceptance is stale; inspect the exact candidate and plan again.")
    text = record["candidate"]
    receipt = {"schema_version": REVISION_VERSION, "author_action": "accept_full_revision",
               "accepted_at_utc": datetime.now(timezone.utc).isoformat(), "revision_record_sha256": record["record_sha256"],
               "plan_sha256": record["plan"]["plan_sha256"], "review_sha256": record["plan"]["review_sha256"],
               "draft_sha256": record["plan"]["draft_sha256"], "final_sha256": candidate_sha256,
               "technical_context_sha256": fingerprint(record["plan"]["technical_context"]),
               "technical_accuracy_sha256": fingerprint(record["technical_accuracy"]),
               "knowledge_context_sha256": fingerprint(record["plan"]["knowledge_context"]),
               "accepted_finding_ids": [row["finding_id"] for row in record["plan"]["findings"] if row["decision"] == "accept"]}
    return text, receipt


def revision_artifacts(record):
    verify_revision(record)
    artifacts = {"revision.json": json_bytes(record), "plan.json": json_bytes(record["plan"]),
                 "technical-context.json": json_bytes(record["plan"]["technical_context"]),
                 "technical-accuracy.json": json_bytes(record["technical_accuracy"]),
                 "validation.json": json_bytes(record["validation"]), "trace.json": json_bytes({"findings": record["trace"]}),
                 "candidate.txt": record["candidate"].encode(), "full.diff": record["full_diff"].encode()}
    for item in record["stages"]:
        if item["stage"] in WRITING_STAGES:
            artifacts["stages/" + item["stage"] + ".txt"] = item["output"]["essay"].encode()
    if record["status"] == "ready":
        artifacts["final-essay.txt"] = record["candidate"].encode()
    if record["rejected_stage"] is not None:
        artifacts["rejected-technical-proposal.json"] = json_bytes(record["rejected_stage"])
    if record["plan"]["knowledge_context"] is not None:
        artifacts["knowledge.json"] = json_bytes(record["plan"]["knowledge_context"])
    artifacts["report.md"] = ("# Full-essay revision\n\nStatus: **" + record["status"] + "**\n\n" +
        ("Checks passed; inspect candidate and full.diff before explicit final acceptance.\n" if record["status"] == "ready" else "Candidate only; final acceptance is blocked.\n") +
        "\n".join("- " + item for item in record["validation"]["blockers"]) + "\n" +
        "\n".join("- " + item for item in record["plan"]["knowledge_warnings"]) + "\n" +
        render_knowledge(record["plan"]["knowledge_context"])).encode()
    return artifacts
