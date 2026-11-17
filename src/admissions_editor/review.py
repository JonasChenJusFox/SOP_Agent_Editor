import difflib
import hashlib
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from .providers import ROLE_NAMES, ReviewerError, review_schema

PARAGRAPH_BREAK = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)(?:[ \t]*\r?\n)*")
NUMBERS = re.compile(r"\d+(?:[.,]\d+)*[+%]?")
FINDING_LIMIT = 12
CHANGE_LIMIT = 240
RUN_VERSION = 1


class ReviewerProvider(Protocol):
    label: str

    def review(self, role: str, review_input: dict) -> dict: ...


def paragraph_spans(draft: str) -> dict[str, tuple[int, str]]:
    spans = {}
    start = 0
    stops = [(match.start(), match.end()) for match in PARAGRAPH_BREAK.finditer(draft)]
    stops.append((len(draft), len(draft)))
    for stop, next_start in stops:
        excerpt = draft[start:stop]
        content = excerpt.strip()
        if content:
            offset = start + len(excerpt) - len(excerpt.lstrip())
            spans[f"P{len(spans) + 1:03d}"] = (offset, content)
        start = next_start
    return spans


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_evidence(directory: Path | None) -> dict[str, str]:
    if directory is None:
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if not path.is_symlink() and path.is_file() and path.suffix in {".md", ".txt"}
    }


def collect_review(
    provider: ReviewerProvider,
    role: str,
    payload_json: str,
    paragraphs: dict[str, tuple[int, str]],
    evidence: dict[str, str],
) -> dict:
    try:
        result = provider.review(role, json.loads(payload_json))
    except ReviewerError as error:
        return {"status": "failed", "findings": [], "diagnostics": [str(error)]}
    except (OSError, ValueError, RuntimeError) as error:
        # Provider errors can contain payloads or credentials. The report stores the class only.
        return {"status": "failed", "findings": [], "diagnostics": [type(error).__name__]}

    validator = Draft202012Validator(review_schema())
    if not validator.is_valid(result) or result["role"] != role:
        return {"status": "failed", "findings": [], "diagnostics": ["Schema or reviewer role mismatch."]}
    if len(result["findings"]) > FINDING_LIMIT:
        return {"status": "failed", "findings": [], "diagnostics": ["Reviewer finding limit exceeded."]}

    findings = []
    diagnostics = []
    for index, finding in enumerate(result["findings"], 1):
        paragraph = paragraphs.get(finding["paragraph_id"])
        quote = finding["quote"]
        if paragraph is None or not quote.strip():
            diagnostics.append(f"Finding {index} has an invalid paragraph or empty quote.")
            continue
        paragraph_start, paragraph_text = paragraph
        location = paragraph_text.find(quote)
        if location < 0 or paragraph_text.find(quote, location + 1) >= 0:
            diagnostics.append(f"Finding {index} does not have a unique exact quotation.")
            continue
        if set(finding["evidence_ids"]) - evidence.keys():
            diagnostics.append(f"Finding {index} refers to an unknown evidence file.")
            continue
        action = finding["action"]
        expected_action = {"preserve": "preserve", "question": "verify"}.get(finding["kind"])
        allowed_actions = {expected_action} if expected_action else {"revise", "verify"}
        if (
            not finding["rationale"].strip()
            or action not in allowed_actions
            or (finding["replacement"] is not None and action != "revise")
        ):
            diagnostics.append(f"Finding {index} has inconsistent editorial fields.")
            continue
        start = paragraph_start + location
        findings.append({
            **finding,
            "id": f"{role}-{index:02d}",
            "role": role,
            "start": start,
            "end": start + len(quote),
        })
    return {
        "status": "degraded" if diagnostics else "complete",
        "findings": findings,
        "diagnostics": diagnostics,
    }


def group_findings(findings: list[dict]) -> list[dict]:
    grouped = {}
    for finding in findings:
        key = (
            finding["start"], finding["end"], finding["category"],
            finding["kind"], finding["action"], finding["replacement"],
        )
        grouped.setdefault(key, []).append(finding)
    return [
        {"id": f"F-{index:03d}", "findings": items}
        for index, (_, items) in enumerate(sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1], repr(item[0]))), 1)
    ]


def overlapping_findings(findings: list[dict]) -> list[dict]:
    conflicts = []
    for index, left in enumerate(findings):
        for right in findings[index + 1:]:
            if max(left["start"], right["start"]) >= min(left["end"], right["end"]):
                continue
            actions = {left["action"], right["action"]}
            if "revise" not in actions:
                continue
            if actions == {"revise"} and (
                left["start"], left["end"], left["replacement"]
            ) == (right["start"], right["end"], right["replacement"]):
                continue
            conflicts.append({"finding_ids": [left["id"], right["id"]]})
    return conflicts


def eligible_changes(
    findings: list[dict], conflicts: list[dict], complete: bool, draft_hash: str,
    requirements_hash: str,
) -> list[dict]:
    if not complete:
        return []
    blocked = {finding_id for conflict in conflicts for finding_id in conflict["finding_ids"]}
    changes = []
    seen = set()
    for finding in findings:
        quote, replacement = finding["quote"], finding["replacement"]
        if (
            finding["id"] in blocked
            or finding["role"] != "language"
            or finding["category"] != "language"
            or finding["kind"] != "issue"
            or finding["severity"] != "low"
            or finding["action"] != "revise"
            or finding["evidence_ids"]
            or replacement is None
            or not replacement.strip()
            or replacement == quote
            or max(len(quote), len(replacement)) > CHANGE_LIMIT
            or any(char in quote + replacement for char in "\n\r\x00")
            or NUMBERS.findall(quote) != NUMBERS.findall(replacement)
        ):
            continue
        edit = {key: finding[key] for key in ("start", "end", "quote", "replacement")}
        payload = json.dumps([draft_hash, requirements_hash, edit], sort_keys=True).encode("utf-8")
        change_id = f"CHG-{sha256(payload)[:16]}"
        if change_id not in seen:
            changes.append({"id": change_id, "finding_id": finding["id"], **edit})
            seen.add(change_id)
    return changes


def apply_changes(draft: str, changes: list[dict]) -> str:
    if not changes:
        return draft
    pieces = []
    cursor = 0
    for change in sorted(changes, key=lambda item: item["start"]):
        start, end = change["start"], change["end"]
        if not (cursor <= start < end <= len(draft)) or draft[start:end] != change["quote"]:
            raise ValueError("A selected change is stale, invalid, or overlaps another change.")
        pieces.extend([draft[cursor:start], change["replacement"]])
        cursor = end
    return "".join(pieces) + draft[cursor:]


def unified_diff(original: str, revised: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        revised.splitlines(keepends=True),
        fromfile="original",
        tofile="proposed",
    )
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)


def run_review(
    provider: ReviewerProvider, draft_bytes: bytes, requirements: str,
    preferences: str, evidence: dict[str, str],
) -> dict:
    draft = draft_bytes.decode("utf-8")
    paragraphs = paragraph_spans(draft)
    if not paragraphs or not requirements.strip():
        raise ValueError("Draft and application requirements must contain text.")
    if "\x00" in draft:
        raise ValueError("The draft must be a plain UTF-8 text file.")

    payload_json = json.dumps({
        "draft": draft,
        "paragraphs": [{"id": key, "text": span[1]} for key, span in paragraphs.items()],
        "requirements": requirements,
        "preferences": preferences,
        "evidence": evidence,
    }, ensure_ascii=False)

    with ThreadPoolExecutor(max_workers=len(ROLE_NAMES)) as pool:
        jobs = {
            role: pool.submit(collect_review, provider, role, payload_json, paragraphs, evidence)
            for role in ROLE_NAMES
        }
        reviewers = {role: job.result() for role, job in jobs.items()}

    complete = all(result["status"] == "complete" for result in reviewers.values())
    findings = [finding for result in reviewers.values() for finding in result["findings"]]
    conflicts = overlapping_findings(findings)
    draft_hash = sha256(draft_bytes)
    requirements_hash = sha256(requirements.encode("utf-8"))
    return {
        "schema_version": RUN_VERSION,
        "status": "complete" if complete else "partial",
        "provider": provider.label,
        "draft_sha256": draft_hash,
        "requirements_sha256": requirements_hash,
        "preferences_sha256": sha256(preferences.encode("utf-8")),
        "evidence_sha256": {
            key: sha256(value.encode("utf-8")) for key, value in evidence.items()
        },
        "reviewers": reviewers,
        "groups": group_findings(findings),
        "conflicts": conflicts,
        "changes": eligible_changes(findings, conflicts, complete, draft_hash, requirements_hash),
        "diagnostics": [
            f"{role}: {item}" for role, result in reviewers.items() for item in result["diagnostics"]
        ],
    }


def markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()!#|~])", r"\\\1", html.escape(" ".join(value.split()), quote=False))


def render_report(run: dict) -> str:
    lines = [
        "# Editorial Report", "", f"- Run status: **{run['status']}**",
        f"- Provider: {markdown_text(run['provider'])}",
        f"- Draft SHA-256: `{run['draft_sha256']}`",
        "- Evidence references identify supplied files. Their existence does not establish factual support.",
        "- Repeated reviewer observations describe overlap in findings. Each still needs editorial judgment.",
        "",
        "## Reviewer coverage", "",
    ]
    for role, result in run["reviewers"].items():
        count = len(result["findings"])
        noun = "finding" if count == 1 else "findings"
        lines.append(f"- **{role}**: {result['status']}, {count} anchored {noun}")
        lines.extend(f"  - {item}" for item in result["diagnostics"])
    if run["status"] == "partial":
        lines.extend(["", "Approval is disabled for this run. Resolve the reviewer failures and review again."])

    sections = (
        ("Issues raised by multiple reviewers", lambda f, n: f["kind"] == "issue" and n > 1),
        ("Issues raised by one reviewer", lambda f, n: f["kind"] == "issue" and n == 1),
        ("Passages to preserve", lambda f, n: f["kind"] == "preserve"),
        ("Questions for the author", lambda f, n: f["kind"] == "question"),
    )
    for heading, include in sections:
        lines.extend(["", f"## {heading}", ""])
        for group in run["groups"]:
            findings = group["findings"]
            first = findings[0]
            if not include(first, len({item["role"] for item in findings})):
                continue
            lines.extend([
                f"### {group['id']} · {markdown_text(first['paragraph_id'])} · {first['category']}",
                "", f"> {markdown_text(first['quote'])}", "",
            ])
            for finding in findings:
                rationale = markdown_text(finding["rationale"])
                lines.append(
                    f"- **{finding['id']}** ({finding['severity']}, {finding['action']}): {rationale}"
                )
                if finding["replacement"] is not None:
                    lines.append(f"  - Suggested wording: {markdown_text(finding['replacement'])}")
                if finding["evidence_ids"]:
                    refs = ", ".join(markdown_text(item) for item in finding["evidence_ids"])
                    lines.append(f"  - Supplied evidence references: {refs}")
            lines.append("")
        if lines[-1] == "" and lines[-2] == f"## {heading}":
            lines.append("None reported.")

    lines.extend(["", "## Overlapping recommendations", ""])
    lines.extend(
        [f"- Manual decision: {' / '.join(item['finding_ids'])}" for item in run["conflicts"]]
        or ["None detected by the exact-span rules."]
    )
    lines.extend(["", "## Changes eligible for explicit approval", ""])
    for change in run["changes"]:
        lines.extend([
            f"- `{change['id']}` · {change['finding_id']}",
            f"  - Before: {markdown_text(change['quote'])}",
            f"  - After: {markdown_text(change['replacement'])}",
        ])
    if not run["changes"]:
        lines.append("None available.")
    return "\n".join(lines) + "\n"
