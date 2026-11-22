"""Independent adjudication, audit records and bounded review rounds."""

import copy
import json
import re
from collections import Counter
from datetime import datetime, timezone

from .contracts import SCHEMAS, ContractError, anchor, evidence_ids, validate
from .facts import build_ledger, candidate_details
from .providers import ROLE_NAMES, ReviewerError
from .review import (PROMPT_VERSION, RULES_VERSION, RUN_VERSION, collect_review, group_findings,
                     eligible_changes, fingerprint, overlapping_findings, paragraph_spans, sha256,
                     knowledge_evidence, validate_knowledge_context)

WORKFLOW_PROMPT_VERSION = "workflow-3"
TASK_PROMPT_VERSIONS = {"observe": "observe-3", "decide": "decide-3", "generate": "generate-2", "targeted": "targeted-3"}


class RuleWorkflowProvider:
    """Transparent offline substitute; no claim of model or human judgment."""

    label = "rule:offline-1"

    def task(self, task, payload, schema):
        if task == "observe":
            return {"observations": [{"paragraph_id": c["paragraph_id"], "quote": c["quote"],
                      "assessment": f"Ledger status: {c['status']}; do not infer truth from missing evidence.",
                      "evidence_ids": sorted({r["source_id"] for r in c["citations"]})}
                     for c in payload["ledger"]["claims"]]}
        if task == "decide":
            return {"decisions": [{"group_id": g["id"], "paragraph_id": g["paragraph_id"],
                     "quote": g["quote"], "decision": "author_decision" if g["disagreement"] else
                     ("verify" if g["action"] == "verify" else "preserve" if g["action"] == "preserve" else "adopt"),
                     "rationale": "Offline rule recommendation; human review required. No voting or semantic entailment inferred.",
                     "evidence_ids": g["evidence_ids"]} for g in payload["groups"]]}
        if task == "generate":
            return {"paragraphs": [{"text": span[1], "material_ids": [m["id"]]}
                                   for m in payload["materials"] for span in paragraph_spans(m["text"]).values()],
                    "missing_information": payload["questions"]}
        if task == "targeted":
            return {"checks": [{"issue_id": i["issue_id"], "status": "deferred",
                                "rationale": "Offline rule cannot establish semantic resolution; author confirmation required.",
                                "evidence_ids": []} for i in payload["previous_issues"]]}
        raise ValueError("Unknown workflow task.")


def audit_event(events, kind, data):
    event = {"index": len(events) + 1, "kind": kind, "data": data,
             "previous_sha256": events[-1]["sha256"] if events else None}
    events.append({**event, "sha256": fingerprint(event)})


def seal_run(run):
    run.pop("record_sha256", None)
    run["record_sha256"] = fingerprint(run)
    return run


def verify_run(run):
    """Check record integrity and revalidate each proposal from the frozen input.

    Checksums detect accidental edits, not a malicious local user who can rewrite
    the whole record. No external signature/trust service is implied.
    """
    try:
        if run["schema_version"] != RUN_VERSION or run["prompt_version"] != PROMPT_VERSION or run["rules_version"] != RULES_VERSION:
            raise ValueError("Unsupported run/prompt/rules version; review again.")
        record = {k: v for k, v in run.items() if k != "record_sha256"}
        if run["record_sha256"] != fingerprint(record):
            raise ValueError("The review record checksum differs; review again.")
        snapshot = run["snapshot"]
        knowledge = validate_knowledge_context(snapshot.get("knowledge_context"))
        if any(snapshot["evidence"].get(key) != excerpt for key, excerpt in knowledge_evidence(knowledge).items()):
            raise ValueError("Retrieved repository excerpts differ from frozen evidence.")
        expected = fingerprint({"snapshot": snapshot, "context": run["context"],
                                "prompt_version": PROMPT_VERSION, "rules_version": RULES_VERSION})
        if expected != run["input_sha256"] or sha256(snapshot["draft"].encode()) != run["draft_sha256"]:
            raise ValueError("The frozen input is inconsistent.")
        if sha256(snapshot["requirements"].encode()) != run["requirements_sha256"] or \
                sha256(snapshot["preferences"].encode()) != run["preferences_sha256"] or \
                {k: sha256(v.encode()) for k, v in snapshot["evidence"].items()} != run["evidence_sha256"]:
            raise ValueError("The frozen evidence or requirements are inconsistent.")
        expected_paragraphs = [{"id": k, "text": v[1]} for k, v in paragraph_spans(snapshot["draft"]).items()]
        if snapshot["paragraphs"] != expected_paragraphs:
            raise ValueError("Frozen paragraph locations are inconsistent.")
        previous = None
        for index, event in enumerate(run["audit"], 1):
            if event["index"] != index or event["previous_sha256"] != previous or event["sha256"] != fingerprint({k: v for k, v in event.items() if k != "sha256"}):
                raise ValueError("The audit chain is inconsistent.")
            previous = event["sha256"]
    except (KeyError, TypeError, AttributeError):
        raise ValueError("The review record is malformed.") from None


def disagreements(run):
    """Conservative span alignment; every alignment records its mechanical basis."""
    findings = [f for r in run["reviewers"].values() for f in r["findings"]]
    results = []
    for index, left in enumerate(findings):
        for right in findings[index + 1:]:
            if max(left["start"], right["start"]) >= min(left["end"], right["end"]):
                continue
            kinds = []
            if left["replacement"] != right["replacement"] and (left["replacement"] is not None or right["replacement"] is not None):
                kinds.append("overlapping_edits")
            if left["action"] != right["action"]:
                kinds.append("opposite_actions")
            if {left["kind"], right["kind"]} == {"issue", "preserve"} or (left["category"] == right["category"] == "evidence" and left["kind"] != right["kind"]):
                kinds.append("fact_conclusion_requires_review")
            if left["category"] != right["category"] or left["rationale"] != right["rationale"]:
                kinds.append("interpretation_requires_review")
            if kinds:
                results.append({"finding_ids": [left["id"], right["id"]], "types": kinds,
                                "alignment": "overlapping exact source spans", "needs_human_review": True})
    return results


def run_task(provider, task, payload):
    # Serialize before calling providers to prevent task implementations from mutating snapshots.
    frozen = json.dumps(payload, ensure_ascii=False)
    result = provider.task(task, json.loads(frozen), SCHEMAS[task])
    validate(result, SCHEMAS[task], task)
    return result, {"task": task, "provider": provider.label, "request_sha256": sha256(frozen.encode()),
                    "response_sha256": fingerprint(result), "prompt_version": TASK_PROMPT_VERSIONS[task],
                    "metrics": getattr(provider, "call_records", {}).get(task, {})}


def check_observations(snapshot, result):
    """Recheck both new and cached observations against the same frozen draft."""
    path = "response"
    try:
        validate(result, SCHEMAS["observe"], "observe")
        for index, item in enumerate(result["observations"]):
            path = f"observations[{index}].quote"
            anchor(snapshot, item)
            path = f"observations[{index}].evidence_ids"
            evidence_ids(snapshot, item["evidence_ids"])
            path = f"observations[{index}].assessment"
            if not item["assessment"].strip():
                raise ContractError("empty_assessment")
    except ContractError as error:
        error.path = path
        raise


def observation_cache_valid(snapshot, ledger, provider_label, cache):
    try:
        first, record = cache["output"], cache["request"]
        expected_request = sha256(json.dumps({"snapshot": snapshot, "ledger": ledger}, ensure_ascii=False).encode())
        if (record["task"] != "observe" or record["provider"] != provider_label or
                record["prompt_version"] != TASK_PROMPT_VERSIONS["observe"] or
                record["request_sha256"] != expected_request or record["response_sha256"] != fingerprint(first)):
            return False
        check_observations(snapshot, first)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def cached_observation(source, provider_label, source_sha256):
    adjudication = source["adjudication"]
    record = next((r for r in adjudication.get("requests", []) if r.get("task") == "observe"), None)
    cache = {"output": adjudication.get("independent_observations"), "request": record,
             "source_run_sha256": source_sha256}
    return cache if observation_cache_valid(source["snapshot"], source["ledger"], provider_label, cache) else None


def adjudicate(run, provider, reverse=False, observation_cache=None):
    snapshot = run["snapshot"]
    records = []
    first = None
    second = None
    called = []
    coverage = None
    stage = "observe"
    path = "response"
    try:
        if observation_cache is not None:
            if not observation_cache_valid(snapshot, run["ledger"], provider.label, observation_cache):
                raise ContractError("invalid_cached_observation")
            first = copy.deepcopy(observation_cache["output"])
            record = {**copy.deepcopy(observation_cache["request"]), "reused": True,
                      "reused_from_run_sha256": observation_cache["source_run_sha256"]}
        else:
            first_payload = {"snapshot": snapshot, "ledger": run["ledger"]}
            called.append("observe")
            first, record = run_task(provider, "observe", first_payload)
        records.append(record)
        check_observations(snapshot, first)
        blocked = {i for d in run["disagreements"] for i in d["finding_ids"]}
        groups = []
        labels = set()
        for reviewer in run["reviewers"].values():
            label = reviewer.get("provider", "")
            if label and label not in {"fixture", "role-routing"}:
                labels.update((label, label.removeprefix("openai:")))

        def anonymous(text):
            for label in sorted(labels, key=len, reverse=True):
                text = text.replace(label, "[hidden model]")
            return text

        for group in run["groups"]:
            first_finding = group["findings"][0]
            opinions = []
            for finding in group["findings"]:
                # Allow-list: model, writer identity, raw prompts and vote counts cannot enter stage 2.
                opinions.append({key: anonymous(finding[key]) if isinstance(finding[key], str) else finding[key]
                                 for key in ("role", "kind", "category", "severity", "action", "rationale", "replacement")})
            if reverse:
                opinions.reverse()
            groups.append({"id": group["id"], "paragraph_id": first_finding["paragraph_id"],
                           "quote": first_finding["quote"], "action": first_finding["action"],
                           "evidence_ids": sorted({s for f in group["findings"] for s in f["evidence_ids"]}),
                           "disagreement": any(f["id"] in blocked for f in group["findings"]),
                           "opinions": opinions})
        if reverse:
            groups.reverse()
        stage, path = "decide", "response"
        called.append("decide")
        second, record = run_task(provider, "decide", {"snapshot": snapshot, "ledger": run["ledger"],
                                  "independent_observations": first, "groups": groups})
        records.append(record)
        by_group = {g["id"]: g for g in groups}
        decisions = second["decisions"]
        path = "decisions"
        if len(decisions) != len(groups) or {d["group_id"] for d in decisions} != set(by_group):
            counts = Counter(d["group_id"] for d in decisions)
            coverage = {"expected_count": len(groups), "returned_count": len(decisions),
                        "missing_group_ids": sorted(set(by_group) - set(counts)),
                        "duplicate_group_ids": sorted(k for k, n in counts.items() if n > 1 and k in by_group),
                        "unexpected_group_count": len(set(counts) - set(by_group))}
            raise ContractError("incomplete_decisions")
        for index, item in enumerate(decisions):
            path = f"decisions[{index}].quote"
            anchor(snapshot, item)
            path = f"decisions[{index}].evidence_ids"
            evidence_ids(snapshot, item["evidence_ids"])
            group = by_group[item["group_id"]]
            path = f"decisions[{index}].anchor_or_rationale"
            if (item["paragraph_id"], item["quote"]) != (group["paragraph_id"], group["quote"]) or not item["rationale"].strip():
                raise ContractError("decision_anchor_mismatch")
        return {"status": "complete", "mode": provider.label, "independent_observations": first,
                "decisions": decisions, "requests": records,
                "observation_reused": observation_cache is not None,
                "call_metrics": {task: copy.deepcopy(provider.call_records[task]) for task in called
                                 if task in getattr(provider, "call_records", {})},
                "opinion_order": "reverse" if reverse else "forward"}
    except Exception as error:
        # Fail closed but preserve the partial review and successful first stage.
        diagnostic = {"stage": stage, "path": getattr(error, "path", path) if isinstance(error, ContractError) else path,
                      "error_type": type(error).__name__}
        if isinstance(error, ContractError):
            diagnostic.update({"code": error.code, "message": str(error)})
        if coverage is not None:
            diagnostic["coverage"] = coverage
        return {"status": "failed", "mode": provider.label, "decisions": [], "requests": records,
                "independent_observations": first,
                "observation_reused": observation_cache is not None,
                "unvalidated_decisions": second["decisions"] if second is not None else None,
                "call_metrics": {task: copy.deepcopy(provider.call_records[task]) for task in called
                                 if task in getattr(provider, "call_records", {})},
                "diagnostics": [type(error).__name__], "validation_errors": [diagnostic]}


def issue_key(finding):
    return fingerprint([finding["category"], finding.get("requirement_id") or " ".join(finding["quote"].lower().split())])


def track_issues(run, parent, provider, approved_ids):
    previous = parent.get("issues", []) if parent else []
    prior = {i["key"]: i for i in previous}
    issues = []
    current_by_key = {}
    for group in run["groups"]:
        f = group["findings"][0]
        if f["kind"] == "preserve":
            continue
        key = issue_key(f)
        severity = max((i["severity"] for i in group["findings"]), key={"low": 0, "medium": 1, "high": 2}.get)
        if key in current_by_key:
            item = current_by_key[key]
            item["group_ids"].append(group["id"])
            if {"low": 0, "medium": 1, "high": 2}[severity] > {"low": 0, "medium": 1, "high": 2}[item["severity"]]:
                item["severity"] = severity
            continue
        old = prior.get(key)
        item = {"issue_id": old["issue_id"] if old else "ISS-" + key[:16], "key": key,
                       "group_id": group["id"], "category": f["category"], "quote": f["quote"],
                       "group_ids": [group["id"]], "alignment": "category and normalized exact quotation; inspect group spans",
                       "paragraph_id": f["paragraph_id"],
                       "severity": severity,
                       "status": ("recurring" if old["status"] == "resolved" else "persistent") if old else "new",
                       "history": (old.get("history", []) if old else []) + [{"run_id": run["run_id"], "group_id": group["id"]}]}
        if old and old.get("author_decision"):
            item["author_decision"] = old["author_decision"]
        issues.append(item)
        current_by_key[key] = item
    checks = {"status": "not_needed", "checks": []}
    if previous:
        try:
            response, request = run_task(provider, "targeted", {"snapshot": run["snapshot"], "ledger": run["ledger"],
                    "previous_issues": previous, "blind_findings": run["groups"], "approved_change_ids": approved_ids})
            if len(response["checks"]) != len(previous) or {c["issue_id"] for c in response["checks"]} != {i["issue_id"] for i in previous}:
                raise ValueError("Targeted review must check each previous issue exactly once.")
            for item in response["checks"]:
                evidence_ids(run["snapshot"], item["evidence_ids"])
                if not item["rationale"].strip():
                    raise ValueError("Targeted review needs a rationale.")
            checks = {"status": "complete", **response, "request": request}
        except Exception as error:
            checks = {"status": "failed", "checks": [], "diagnostics": [type(error).__name__]}
    current = {i["key"] for i in issues}
    for old in previous:
        if old["key"] in current:
            continue
        check = next((c for c in checks["checks"] if c["issue_id"] == old["issue_id"]), None)
        matching_changes = [c for c in parent["changes"] if c["id"] in approved_ids and c["quote"] == old["quote"]]
        # A vanished quote alone does not prove resolution. Exact approved local language
        # corrections can be mechanically closed; semantic closure remains a recommendation.
        resolved = bool(matching_changes) and old["category"] == "language"
        state = "resolved" if resolved else "resolved" if old["status"] == "resolved" else "deferred"
        issues.append({**old, "group_id": None, "status": state, "targeted_check": check,
                       "resolution_basis": "approved_local_language_change" if resolved else "awaiting_author_confirmation",
                       "history": old.get("history", []) + [{"run_id": run["run_id"], "group_id": None}]})
    run["issues"], run["targeted_review"] = issues, checks


def enrich_run(run, workflow_provider, *, manifest=None, source_metadata=None, as_of=None,
               parent=None, parent_sha=None, approved_ids=None, max_rounds=3, author_ready=False, reverse=False):
    if not 1 <= max_rounds <= 20:
        raise ValueError("max_rounds must be between 1 and 20.")
    number = parent["round"]["number"] + 1 if parent else 1
    if number > max_rounds:
        raise ValueError("Review round limit reached. Author must explicitly raise --max-rounds to continue.")
    run["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    run["ledger"] = build_ledger(run["snapshot"], manifest, source_metadata, as_of)
    run["disagreements"] = disagreements(run)
    run["adjudication"] = adjudicate(run, workflow_provider, reverse)
    if run["adjudication"]["status"] != "complete":
        run["status"], run["changes"] = "partial", []
        run["diagnostics"].append("Adjudication failed; approval disabled.")
    else:
        run["changes"] = adjudicated_changes(run, run["changes"])
    track_issues(run, parent, workflow_provider, approved_ids or [])
    if run["targeted_review"]["status"] == "failed":
        run["status"], run["changes"] = "partial", []
        run["diagnostics"].append("Targeted review failed; approval disabled.")
    run["candidate_details"] = candidate_details(run)
    new_high = sum(i["status"] in {"new", "recurring"} and i["severity"] == "high" for i in run["issues"])
    quiet = 0 if new_high else (parent["round"].get("quiet_rounds", 0) if parent else 0) + 1
    run["round"] = {"number": number, "max_rounds": max_rounds, "parent_run_sha256": parent_sha,
                    "parent_run_id": parent["run_id"] if parent else None, "quiet_rounds": quiet,
                    "new_high_priority_count": new_high, "author_ready": author_ready}
    update_stop_reason(run)
    run["audit"] = []
    for kind, data in (
        ("freeze_input", {"input_sha256": run["input_sha256"], "parent_run_sha256": parent_sha}),
        ("independent_review", {r: {"status": v["status"], "request_sha256": v["request_sha256"],
                                   "provider": v["provider"]} for r, v in run["reviewers"].items()}),
        ("verify_facts", {"ledger_sha256": fingerprint(run["ledger"])}),
        ("independent_adjudication", {"adjudication_sha256": fingerprint(run["adjudication"])}),
        ("propose_changes", {"changes_sha256": fingerprint(run["changes"])}),
        ("track_round", {"issues_sha256": fingerprint(run["issues"]), **run["round"]}),
    ):
        audit_event(run["audit"], kind, data)
    return seal_run(run)


def revalidate_reviewers(run):
    """Recheck cached reviewer outputs without making provider calls."""
    verify_run(run)
    if set(run["reviewers"]) != set(ROLE_NAMES):
        raise ValueError("All four reviewers must be present.")
    snapshot = run["snapshot"]
    payload_json = json.dumps(snapshot, ensure_ascii=False)
    findings = []
    for role in ROLE_NAMES:
        result = run["reviewers"][role]
        if result["status"] != "complete" or result["diagnostics"]:
            raise ValueError("All four reviewers must complete before approval.")

        class Replay:
            label = "revalidation"

            def review(self, requested_role, payload):
                return copy.deepcopy(result["output"])

        checked = collect_review(Replay(), role, payload_json, paragraph_spans(snapshot["draft"]), snapshot["evidence"])
        if checked["status"] != "complete" or checked["findings"] != result["findings"] or \
                checked["instructions_sha256"] != result["instructions_sha256"] or \
                checked["request_sha256"] != result["request_sha256"]:
            raise ValueError("Stored findings differ from revalidated reviewer output.")
        findings.extend(checked["findings"])
    return findings


def validated_changes(run):
    findings = revalidate_reviewers(run)
    if run["status"] != "complete":
        raise ValueError("Approval requires a complete four-role review and adjudication.")
    changes = eligible_changes(findings, overlapping_findings(findings), True, run["draft_sha256"],
                               run["requirements_sha256"], run["input_sha256"])
    if group_findings(findings) != run["groups"] or overlapping_findings(findings) != run["conflicts"]:
        raise ValueError("Stored finding groups or conflicts differ from revalidated output.")
    if run["adjudication"]["status"] != "complete" or run["targeted_review"]["status"] == "failed":
        raise ValueError("Approval requires successful adjudication and follow-up review.")
    changes = adjudicated_changes(run, changes)
    if changes != run["changes"]:
        raise ValueError("Stored proposals differ from eligible proposals.")
    return changes


def update_stop_reason(run):
    state = run["round"]
    unresolved = any(i["status"] != "resolved" for i in run["issues"])
    pending_facts = any(c["status"] != "supported" for c in run["ledger"]["claims"])
    reason = "continue_with_author_approval"
    if run["status"] != "complete":
        reason = "pause_incomplete_review"
    elif any(i["status"] == "recurring" for i in run["issues"]):
        reason = "pause_recurring_issues"
    elif state["number"] == state["max_rounds"]:
        reason = "pause_round_limit"
    elif pending_facts:
        reason = "pause_missing_or_conflicting_evidence"
    elif state["quiet_rounds"] >= 2:
        reason = "pause_two_quiet_rounds"
    if state["author_ready"] and not unresolved and not pending_facts and run["status"] == "complete":
        reason = "author_confirmed_ready"
    state["stop_reason"] = reason


def retry_adjudication(source, provider, source_sha256):
    """Fork the same review round, preserving validated blind reviewer results.

    This never invokes a Reviewer or changes source evidence. A new run/checksum
    binds the retry, its workflow configuration and its candidate IDs.
    """
    findings = revalidate_reviewers(source)
    if source["adjudication"]["status"] != "failed":
        raise ValueError("Retry requires a run whose adjudication failed.")
    if source["targeted_review"]["status"] == "failed":
        raise ValueError("Targeted review also failed; run a full follow-up review.")
    run = copy.deepcopy(source)
    run.pop("record_sha256", None)
    run["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    run["context"].update({"workflow_provider": provider.label, "workflow_prompt_version": WORKFLOW_PROMPT_VERSION,
                           "retried_from_run_sha256": source_sha256})
    run["input_sha256"] = fingerprint({"snapshot": run["snapshot"], "context": run["context"],
                                       "prompt_version": PROMPT_VERSION, "rules_version": RULES_VERSION})
    run["run_id"] = "RUN-" + run["input_sha256"][:20]
    for result in run["reviewers"].values():
        result["reused_from_run_sha256"] = source_sha256
    run["groups"] = group_findings(findings)
    run["conflicts"] = overlapping_findings(findings)
    run["disagreements"] = disagreements(run)
    observation = cached_observation(source, provider.label, source_sha256)
    run["adjudication"] = adjudicate(run, provider, run["context"].get("reverse_opinions", False),
                                     observation_cache=observation)
    run["status"] = "complete" if run["adjudication"]["status"] == "complete" else "partial"
    run["changes"] = eligible_changes(findings, run["conflicts"], run["status"] == "complete",
                                      run["draft_sha256"], run["requirements_sha256"], run["input_sha256"])
    run["changes"] = adjudicated_changes(run, run["changes"])
    run["diagnostics"] = [] if run["status"] == "complete" else ["Adjudication failed; approval disabled."]
    run["candidate_details"] = candidate_details(run)
    for issue in run["issues"]:
        issue["history"].append({"run_id": run["run_id"], "group_id": issue["group_id"]})
    update_stop_reason(run)
    run["recovery"] = {"source_run_sha256": source_sha256, "source_run_id": source["run_id"],
                       "reused_reviewers": list(ROLE_NAMES), "new_reviewer_calls": 0,
                       "reused_observation": observation is not None,
                       "workflow_prompt_version": WORKFLOW_PROMPT_VERSION}
    run["audit"] = []
    audit_event(run["audit"], "reuse_validated_review", {**run["recovery"],
                "source_audit_head_sha256": source["audit"][-1]["sha256"], "input_sha256": run["input_sha256"]})
    audit_event(run["audit"], "retry_adjudication", {"adjudication_sha256": fingerprint(run["adjudication"])})
    audit_event(run["audit"], "propose_changes", {"changes_sha256": fingerprint(run["changes"])})
    audit_event(run["audit"], "track_round", {"issues_sha256": fingerprint(run["issues"]), **run["round"]})
    return seal_run(run)


def adjudicated_changes(run, changes):
    adopted = {d["group_id"] for d in run["adjudication"]["decisions"] if d["decision"] == "adopt"}
    finding_ids = {f["id"] for g in run["groups"] if g["id"] in adopted for f in g["findings"]}
    return [c for c in changes if c["finding_id"] in finding_ids]


def render_workflow(run):
    lines = ["", "## Fact ledger", "", run["ledger"]["coverage"], ""]
    for c in run["ledger"]["claims"]:
        lines.append(f"- `{c['id']}` · {c['type']} · **{c['status']}** · {c['paragraph_id']}")
        lines.append(f"  - Verification: {c['verification_method']}; scope: {c['scope']}")
        for ref in c["citations"]:
            lines.append(f"  - `{ref['source_id']}` · {ref['relation']} · expired={ref['expired']} · scope_match={ref['scope_match']}")
            from .review import markdown_text
            lines.append(f"    > {markdown_text(ref['quote'])}")
    lines += ["", "## Two-stage adjudication", "", f"Mode: `{run['adjudication']['mode']}`; status: {run['adjudication']['status']}", ""]
    for error in run["adjudication"].get("validation_errors", []):
        lines.append(f"- Failed at {error['stage']}.{error['path']}: {error.get('code', error['error_type'])}")
        if error.get("message"):
            lines.append(f"  - {error['message']}")
        if error.get("coverage"):
            coverage = error["coverage"]
            lines.append(f"  - Expected {coverage['expected_count']}; returned {coverage['returned_count']}; "
                         f"missing: {', '.join(coverage['missing_group_ids']) or 'none'}; "
                         f"duplicate: {', '.join(coverage['duplicate_group_ids']) or 'none'}; "
                         f"unexpected IDs: {coverage['unexpected_group_count']}")
    if run["adjudication"].get("observation_reused"):
        lines.append("- Reused validated independent observations; no new observation request.")
    for d in run["adjudication"]["decisions"]:
        from .review import markdown_text
        lines.append(f"- {d['group_id']}: **{d['decision']}** — {markdown_text(d['rationale'])}")
    lines += ["", "## Disagreements", ""]
    lines += [f"- {' / '.join(d['finding_ids'])}: {', '.join(d['types'])}; alignment: {d['alignment']}" for d in run["disagreements"]] or ["None detected by span rules."]
    lines += ["", "## Round and issue states", "", f"Round {run['round']['number']}/{run['round']['max_rounds']}: {run['round']['stop_reason']}", ""]
    lines += [f"- `{i['issue_id']}` · {i['status']} · {i['severity']} · {i['category']}" for i in run["issues"]]
    lines += ["", "## Item diffs", "", "See `changes/*.diff` and `candidate-details.json` for context, fact links and whitespace word counts.", ""]
    return "\n".join(lines)
