"""Independent structured-output contracts for full-document editorial passes."""

from .contracts import STRING, NULL_STRING, STRINGS, array, choice, obj

WRITING = obj({"essay": STRING, "addressed_finding_ids": STRINGS,
               "used_fact_ids": STRINGS, "unresolved_questions": STRINGS,
               "technical_flags": array(obj({"claim_id": STRING, "reason": STRING}))})
CHECK_NAMES = ("factual_consistency", "requirements", "accepted_findings", "rejected_findings", "voice", "document_type")
VERIFY_REVISION = obj({
    "checks": array(obj({"check": choice(*CHECK_NAMES), "passed": {"type": "boolean"}, "rationale": STRING})),
    "finding_checks": array(obj({"finding_id": STRING, "status": choice("addressed", "preserved", "unresolved"),
                                  "quote": STRING, "rationale": STRING})),
    "claim_changes": array(obj({"original_quote": NULL_STRING, "final_quote": NULL_STRING,
                                 "source_type": choice("original", "confirmed_fact", "evidence"),
                                 "source_id": STRING, "source_quote": STRING,
                                 "status": choice("supported", "unsupported", "uncertain")})),
    "unresolved_questions": STRINGS,
    "technical_checks": array(obj({
        "claim_id": STRING,
        "status": choice("preserved", "supported_change", "not_used", "altered", "unsupported", "ambiguous"),
        "original_quote": STRING, "final_quote": NULL_STRING,
        "source_type": choice("draft", "evidence", "confirmed_fact"), "source_id": STRING,
        "source_quote": STRING, "reason": STRING,
    })),
})
REVISION_SCHEMAS = {"sop_revise": WRITING, "polish": WRITING, "humanize": WRITING,
                    "verify_revision": VERIFY_REVISION}
