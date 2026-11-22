"""Conservative fact inventory and auditable, author-supplied verification links.

Exact quotations validate provenance, not semantic entailment. Only explicitly
attributed human checks can mark support; auto-extracted claims remain pending.
"""

import re
from datetime import date

from .contracts import LEDGER, anchor, validate
from .review import NUMBERS, fingerprint, paragraph_spans, sha256


def automatic_claims(snapshot):
    claims = {}
    for paragraph in snapshot["paragraphs"]:
        for match in re.finditer(r"[^.!?]+(?:[.!?]|$)", paragraph["text"]):
            quote = match.group().strip()
            if not quote:
                continue
            # Multi-label inventory: a sentence may contain both attribution and a metric.
            types = []
            if re.search(r"\bI\s+(?:\w+\s+){0,2}(?:built|wrote|designed|implemented|led|owned|developed)\b", quote, re.I):
                types.append("personal_contribution")
            if re.search(r"\b(our|we|team|system)\b", quote, re.I):
                types.append("team_outcome")
            if NUMBERS.search(quote):
                types.append("number")
            if re.search(r"\b(university|program|professor|laboratory|curriculum|course)\b", quote, re.I):
                types.append("program_info")
            for kind in types:
                # Repeated sentences need a wider unique anchor, not a failed run.
                exact_quote = quote if paragraph["text"].count(quote) == 1 else paragraph["text"]
                claim_id = "AUTO-" + fingerprint([paragraph["id"], exact_quote, kind])[:12]
                claims[claim_id] = {"id": claim_id,
                               "paragraph_id": paragraph["id"], "quote": exact_quote, "type": kind,
                               "subject": "unconfirmed", "action_or_result": quote, "date": None,
                               "value": " / ".join(NUMBERS.findall(quote)) or None, "unit": None,
                               "scope": "unconfirmed", "citations": [], "confirmed_by": None}
    return list(claims.values())


def build_ledger(snapshot, manifest=None, source_metadata=None, as_of=None):
    manifest = manifest or {"claims": []}
    validate(manifest, LEDGER, "Claim manifest")
    source_metadata = source_metadata or {}
    as_of = as_of or date.today().isoformat()
    date.fromisoformat(as_of)
    if set(source_metadata) - snapshot["evidence"].keys():
        raise ValueError("Source metadata refers to an unknown evidence ID.")
    sources = {}
    allowed = {"type", "origin", "retrieved_at", "valid_until", "scope"}
    for source_id, content in snapshot["evidence"].items():
        metadata = source_metadata.get(source_id, {})
        if not isinstance(metadata, dict) or set(metadata) - allowed or any(not isinstance(v, str) for v in metadata.values()):
            raise ValueError("Invalid source metadata fields.")
        for key in ("retrieved_at", "valid_until"):
            if metadata.get(key):
                date.fromisoformat(metadata[key])
        sources[source_id] = {"id": source_id, "type": "unspecified", "origin": source_id,
                              "retrieved_at": "", "valid_until": "", "scope": "unconfirmed",
                              **metadata, "content_sha256": sha256(content.encode()), "content": content}
    explicit = manifest["claims"]
    if len({c["id"] for c in explicit}) != len(explicit) or any(not c["id"].strip() for c in explicit):
        raise ValueError("Claim IDs must be unique and nonempty.")
    covered = {(c["paragraph_id"], c["quote"], c["type"]) for c in explicit}
    claims = explicit + [c for c in automatic_claims(snapshot)
                         if not any(c["paragraph_id"] == p and c["type"] == t and c["quote"] in q
                                    for p, q, t in covered)]
    result = []
    for claim in claims:
        anchor(snapshot, claim)
        citations = []
        for citation in claim["citations"]:
            source = sources.get(citation["source_id"])
            if source is None or not citation["quote"].strip() or citation["quote"] not in source["content"]:
                raise ValueError("Claim verification cites an unknown source or invalid evidence excerpt.")
            # A scope mismatch/expired source cannot establish support in the current context.
            stale = bool(source["valid_until"] and source["valid_until"] < as_of)
            future = bool(source["retrieved_at"] and source["retrieved_at"] > as_of)
            scope_match = citation["scope"] == claim["scope"] and source["scope"] in ("all", claim["scope"])
            citations.append({**citation, "source_sha256": source["content_sha256"],
                              "expired": stale, "not_yet_available": future, "scope_match": scope_match})
        usable = [c for c in citations if not c["expired"] and not c["not_yet_available"] and c["scope_match"]]
        relations = {c["relation"] for c in usable}
        verified = bool(claim["confirmed_by"] and claim["confirmed_by"].strip())
        if not verified or not usable:
            status = "pending_evidence"
        elif "contradicts" in relations:
            status = "source_conflict"
        elif "supports" in relations:
            status = "supported"
        else:
            status = "partially_supported"
        result.append({**claim, "citations": citations, "status": status,
                       "verification_method": "attributed_human_mapping" if verified else "unverified_inventory",
                       "needs_human_review": not verified or status != "supported"})
    return {"version": "ledger-1", "as_of": as_of, "sources": sources, "claims": result,
            "coverage": "Heuristic inventory plus supplied claims; completeness requires human review."}


def candidate_details(run):
    from .review import unified_diff
    eligible = {c["finding_id"]: c for c in run["changes"]}
    details = []
    spans = paragraph_spans(run["snapshot"]["draft"])
    for group in run["groups"]:
        for finding in group["findings"]:
            if finding["replacement"] is None:
                continue
            paragraph_start, before = spans[finding["paragraph_id"]]
            start, end = finding["start"] - paragraph_start, finding["end"] - paragraph_start
            after = before[:start] + finding["replacement"] + before[end:]
            affected = [c["id"] for c in run["ledger"]["claims"]
                        if c["paragraph_id"] == finding["paragraph_id"] and
                        (finding["quote"] in c["quote"] or c["quote"] in finding["quote"])]
            details.append({"finding_id": finding["id"], "group_id": group["id"],
                            "change_id": eligible.get(finding["id"], {}).get("id"),
                            "eligible": finding["id"] in eligible,
                            "reason": "explicit_approval_required" if finding["id"] in eligible else "approval_policy_withheld",
                            "context_before": before, "context_after": after,
                            "word_count_before": len(before.split()), "word_count_after": len(after.split()),
                            "word_count_method": "Unicode whitespace split",
                            "affected_claim_ids": affected, "diff": unified_diff(before, after)})
    return details
