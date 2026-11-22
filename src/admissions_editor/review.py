import difflib
import copy
import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Protocol

from jsonschema import Draft202012Validator

from .providers import ROLE_NAMES, ROLE_FOCUS, REVIEW_RULES, ReviewerError, review_schema

PARAGRAPH_BREAK = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)(?:[ \t]*\r?\n)*")
NUMBERS = re.compile(r"\d+(?:[.,]\d+)*[+%]?")
FINDING_LIMIT = 12
CHANGE_LIMIT = 240
RUN_VERSION = 2
PROMPT_VERSION = "review-2"
RULES_VERSION = "rules-2"


def fingerprint(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


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
    started = time.monotonic()
    metadata = {"provider": getattr(provider, "label_for", lambda _: provider.label)(role),
                "request_sha256": sha256(payload_json.encode()), "prompt_version": PROMPT_VERSION,
                "instructions_sha256": sha256(getattr(provider, "instructions_for", lambda r: f"{REVIEW_RULES}\nRole: {r}. {ROLE_FOCUS[r]}")(role).encode())}
    try:
        result = provider.review(role, json.loads(payload_json))
        metadata["metrics"] = getattr(provider, "metrics_for", lambda _: {})(role)
    except ReviewerError as error:
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
        metadata["metrics"] = getattr(provider, "metrics_for", lambda _: {})(role)
        return {**metadata, "status": "failed", "findings": [], "diagnostics": [str(error)]}
    except Exception as error:
        # Provider errors can contain payloads or credentials. The report stores the class only.
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
        return {**metadata, "status": "failed", "findings": [], "diagnostics": [type(error).__name__]}

    metadata.update({"elapsed_seconds": round(time.monotonic() - started, 6), "output": result})

    validator = Draft202012Validator(review_schema())
    if not validator.is_valid(result) or result["role"] != role:
        # Invalid output is not persisted: it can contain unexpected private data.
        metadata.pop("output")
        return {**metadata, "status": "failed", "findings": [], "diagnostics": ["Schema or reviewer role mismatch."]}
    if len(result["findings"]) > FINDING_LIMIT:
        metadata.pop("output")
        return {**metadata, "status": "failed", "findings": [], "diagnostics": ["Reviewer finding limit exceeded."]}

    findings = []
    diagnostics = []
    for index, finding in enumerate(result["findings"], 1):
        requirement_id = finding.get("requirement_id")
        if requirement_id is not None and requirement_id not in re.findall(r"\b[A-Z][A-Z0-9_-]{2,63}\b", json.loads(payload_json)["requirements"]):
            diagnostics.append(f"Finding {index} refers to an unknown requirement ID.")
            continue
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
            "requirement_id": requirement_id,
            "id": f"{role}-{index:02d}",
            "role": role,
            "start": start,
            "end": start + len(quote),
        })
    return {
        **metadata,
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
            finding.get("requirement_id"),
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
    requirements_hash: str, input_hash: str = "",
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
            # Only article/punctuation/spacing edits can be mechanically shown to
            # preserve content tokens. Other wording remains a reviewable proposal.
            or [w.lower() for w in re.findall(r"\w+", quote) if w.lower() not in {"a", "an", "the"}]
               != [w.lower() for w in re.findall(r"\w+", replacement) if w.lower() not in {"a", "an", "the"}]
        ):
            continue
        edit = {key: finding[key] for key in ("start", "end", "quote", "replacement")}
        payload = json.dumps([draft_hash, requirements_hash, input_hash, edit], sort_keys=True).encode("utf-8")
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


def validate_knowledge_context(context):
    """Validate a frozen retrieval without opening source repositories or SQLite.

    Location metadata records the local retriever's provenance, not an external
    signature or proof that code establishes a personal contribution.
    """
    if context is None:
        return None
    try:
        if context["schema_version"] != "knowledge-context-1" or context["status"] not in {
                "ready", "partial", "missing", "stale", "disabled", "unavailable"}:
            raise ValueError("Unsupported repository knowledge context.")
        if not isinstance(context["snapshot_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", context["snapshot_sha256"]):
            raise ValueError("Repository knowledge context needs a retrieval snapshot hash.")
        if not isinstance(context["sources"], list) or not isinstance(context["claims"], list) or \
                not isinstance(context["passages"], list) or not isinstance(context["diagnostics"], list) or \
                any(not isinstance(item, str) for item in context["diagnostics"]):
            raise ValueError("Repository knowledge context collections are malformed.")
        sources = {source["name"]: source for source in context["sources"]}
        if len(sources) != len(context["sources"]):
            raise ValueError("Repository knowledge source names must be unique.")
        claims = {claim["claim_id"]: claim for claim in context["claims"]}
        if len(claims) != len(context["claims"]):
            raise ValueError("Repository knowledge claim IDs must be unique.")
        passages = {}
        for passage in context["passages"]:
            key, path = passage["id"], passage["file_path"]
            if not isinstance(key, str) or not key.startswith("KB-") or passage["evidence_id"] != key or key in passages:
                raise ValueError("Repository passage IDs must be unique evidence IDs.")
            if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or \
                    ".." in PurePosixPath(path).parts or "\\" in path or passage["path"] != path:
                raise ValueError("Repository passage paths must remain relative to their source.")
            if any(isinstance(passage[field], bool) or not isinstance(passage[field], int) for field in ("line_start", "line_end")) or \
                    not 1 <= passage["line_start"] <= passage["line_end"]:
                raise ValueError("Repository passage line ranges are invalid.")
            if not isinstance(passage["excerpt"], str) or not passage["excerpt"].strip() or "\0" in passage["excerpt"]:
                raise ValueError("Repository passages need exact nonempty text excerpts.")
            for field in ("snapshot_hash", "file_sha256"):
                if not isinstance(passage[field], str) or not re.fullmatch(r"[a-f0-9]{64}", passage[field]):
                    raise ValueError("Repository passages need content snapshot and file hashes.")
            commit = passage["commit_sha"]
            if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40,64}", commit)):
                raise ValueError("Repository commit SHA is malformed.")
            source = sources.get(passage["repository"])
            if source is None or source["snapshot_hash"] != passage["snapshot_hash"] or source["commit_sha"] != commit or \
                    source["status"] in {"missing", "stale", "unavailable", "disabled"}:
                raise ValueError("Repository passage does not belong to an available frozen source.")
            if not isinstance(passage["claim_ids"], list) or set(passage["claim_ids"]) - claims.keys():
                raise ValueError("Repository passage references an unknown essay claim.")
            passages[key] = passage
        for key, claim in claims.items():
            if not isinstance(key, str) or not key or claim["id"] != key or \
                    not isinstance(claim["text"], str) or not claim["text"].strip() or \
                    not isinstance(claim["passage_ids"], list) or len(set(claim["passage_ids"])) != len(claim["passage_ids"]) or \
                    set(claim["passage_ids"]) - passages.keys():
                raise ValueError("Repository knowledge claim mapping is malformed.")
            if any(key not in passages[passage_id]["claim_ids"] for passage_id in claim["passage_ids"]):
                raise ValueError("Repository claim and passage mappings differ.")
        for key, passage in passages.items():
            if any(key not in claims[claim_id]["passage_ids"] for claim_id in passage["claim_ids"]):
                raise ValueError("Repository passage and claim mappings differ.")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Repository knowledge context is malformed.") from None
    return copy.deepcopy(context)


def knowledge_evidence(context):
    frozen = validate_knowledge_context(context)
    return {passage["evidence_id"]: passage["excerpt"] for passage in frozen["passages"]} if frozen else {}


def merge_knowledge_evidence(evidence, context):
    merged = dict(evidence)
    for key, excerpt in knowledge_evidence(context).items():
        if key in merged and merged[key] != excerpt:
            raise ValueError("Repository passage ID conflicts with supplied evidence.")
        merged[key] = excerpt
    return merged


def run_review(
    provider: ReviewerProvider, draft_bytes: bytes, requirements: str,
    preferences: str, evidence: dict[str, str], *, max_workers: int = 4,
    context: dict | None = None,
    knowledge_context: dict | None = None,
) -> dict:
    draft = draft_bytes.decode("utf-8")
    paragraphs = paragraph_spans(draft)
    if not paragraphs or not requirements.strip():
        raise ValueError("Draft and application requirements must contain text.")
    if "\x00" in draft:
        raise ValueError("The draft must be a plain UTF-8 text file.")

    knowledge_context = validate_knowledge_context(knowledge_context)
    evidence = merge_knowledge_evidence(evidence, knowledge_context)
    snapshot = {
        "draft": draft,
        "paragraphs": [{"id": key, "text": span[1]} for key, span in paragraphs.items()],
        "requirements": requirements,
        "preferences": preferences,
        "evidence": evidence,
    }
    if knowledge_context is not None:
        snapshot["knowledge_context"] = knowledge_context
    payload_json = json.dumps(snapshot, ensure_ascii=False)
    context = {**(context or {}), "review_configuration": {
        role: getattr(provider, "label_for", lambda _: provider.label)(role) for role in ROLE_NAMES}}
    input_hash = fingerprint({"snapshot": snapshot, "context": context,
                              "prompt_version": PROMPT_VERSION, "rules_version": RULES_VERSION})

    if not 1 <= max_workers <= 4:
        raise ValueError("max_workers must be between 1 and 4.")
    if max_workers == 1:
        # Also supports WebAssembly runtimes without operating-system threads.
        reviewers = {role: collect_review(provider, role, payload_json, paragraphs, evidence) for role in ROLE_NAMES}
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
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
        "prompt_version": PROMPT_VERSION,
        "rules_version": RULES_VERSION,
        "snapshot": snapshot,
        "context": context,
        "input_sha256": input_hash,
        "run_id": "RUN-" + input_hash[:20],
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
        "changes": eligible_changes(findings, conflicts, complete, draft_hash, requirements_hash, input_hash),
        "diagnostics": [
            f"{role}: {item}" for role, result in reviewers.items() for item in result["diagnostics"]
        ],
    }


def markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()!#|~])", r"\\\1", html.escape(" ".join(value.split()), quote=False))


def render_knowledge(context):
    frozen = validate_knowledge_context(context)
    if frozen is None:
        return ""
    lines = ["", "## Repository knowledge", "", f"- Retrieval status: **{markdown_text(frozen['status'])}**",
             "- Retrieved code, documentation, tests and evaluations are context, not proof of authorship, deployment, measured results or personal ownership."]
    for source in frozen["sources"]:
        version = source.get("commit_sha") or source.get("snapshot_hash") or "no available snapshot"
        lines.append(f"- {markdown_text(source['name'])}: {markdown_text(source['status'])}; version {markdown_text(version)}")
    lines.extend("- " + markdown_text(item) for item in frozen["diagnostics"])
    passages = {item["id"]: item for item in frozen["passages"]}
    for claim in frozen["claims"]:
        lines.extend(["", f"### {markdown_text(claim['claim_id'])}", "", "> " + markdown_text(claim["text"]), ""])
        if not claim["passage_ids"]:
            lines.append("No relevant repository passage retrieved; this is not evidence that the claim is false.")
        for passage_id in claim["passage_ids"]:
            passage = passages[passage_id]
            location = f"{passage['repository']}/{passage['file_path']}:L{passage['line_start']}-L{passage['line_end']}"
            lines.extend([f"- {markdown_text(passage_id)} · {markdown_text(location)}",
                          f"  - Commit: {markdown_text(passage['commit_sha'] or 'not a Git snapshot')}; snapshot: {markdown_text(passage['snapshot_hash'])}", ""])
            lines.extend("> " + markdown_text(line) for line in passage["excerpt"].splitlines())
            lines.append("")
    return "\n".join(lines) + "\n"


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
    return "\n".join(lines) + "\n" + render_knowledge(run["snapshot"].get("knowledge_context"))
