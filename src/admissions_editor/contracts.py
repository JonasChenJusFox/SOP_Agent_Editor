"""Versioned contracts shared by local validators and Structured Outputs."""

from jsonschema import Draft202012Validator


class ContractError(ValueError):
    """Application-owned diagnostic; never includes provider text or credentials."""

    MESSAGES = {
        "invalid_draft_anchor": "Use a unique exact quote from the specified draft paragraph.",
        "unknown_evidence_id": "evidence_ids must use supplied evidence filenames; ledger claim IDs are not evidence IDs.",
        "schema_validation_failed": "The structured output did not satisfy its JSON Schema.",
        "empty_assessment": "Independent observations require a nonempty assessment.",
        "incomplete_decisions": "Adjudication must decide every group exactly once.",
        "decision_anchor_mismatch": "A decision must preserve its group's exact draft anchor and include a rationale.",
        "invalid_cached_observation": "Cached observations do not match this input, provider or observation prompt version.",
    }

    def __init__(self, code):
        super().__init__(self.MESSAGES[code])
        self.code = code


def obj(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


def array(items):
    return {"type": "array", "items": items}


STRING = {"type": "string"}
NULL_STRING = {"type": ["string", "null"]}
STRINGS = array(STRING)


def choice(*values):
    return {"type": "string", "enum": list(values)}


ANCHOR = {"paragraph_id": STRING, "quote": STRING}
CITATION = obj({"source_id": STRING, "quote": STRING,
                "relation": choice("supports", "partial", "contradicts"), "scope": STRING})
CLAIM = obj({"id": STRING, **ANCHOR,
             "type": choice("personal_contribution", "team_outcome", "number", "program_info", "interpretation"),
             "subject": STRING, "action_or_result": STRING, "date": NULL_STRING,
             "value": NULL_STRING, "unit": NULL_STRING, "scope": STRING,
             "citations": array(CITATION), "confirmed_by": NULL_STRING})
LEDGER = obj({"claims": array(CLAIM)})
OBSERVATION = obj({**ANCHOR, "assessment": STRING, "evidence_ids": STRINGS})
OBSERVE = obj({"observations": array(OBSERVATION)})
DECISION = obj({"group_id": STRING, "decision": choice("adopt", "preserve", "verify", "author_decision"),
                **ANCHOR, "rationale": STRING, "evidence_ids": STRINGS})
DECIDE = obj({"decisions": array(DECISION)})


def decision_wire_schema(groups):
    """Require each group as a unique object key in the model-facing contract.

    Group identity and draft anchors come from the frozen input, not generation.
    The stored/fixture contract remains the versioned decisions array.
    """
    ids = [group["id"] for group in groups]
    if len(set(ids)) != len(ids):
        raise ValueError("Decision groups must have unique IDs.")
    return obj({"decisions": obj({group_id: obj({
        "decision": choice("adopt", "preserve", "verify", "author_decision"),
        "rationale": STRING, "evidence_ids": array(STRING),
    }) for group_id in ids})})


def normalize_decisions(result, groups, wire_schema):
    validate(result, wire_schema, "decide")
    return {"decisions": [{"group_id": group["id"], "paragraph_id": group["paragraph_id"],
                           "quote": group["quote"], **result["decisions"][group["id"]]}
                          for group in groups]}
MATERIAL = obj({"id": STRING, "text": STRING, "confirmed": {"type": "boolean"}})
MATERIALS = obj({"materials": array(MATERIAL), "questions": STRINGS})
GENERATE = obj({"paragraphs": array(obj({"text": STRING, "material_ids": STRINGS})),
                "missing_information": STRINGS})
TARGETED = obj({"checks": array(obj({"issue_id": STRING,
                "status": choice("resolved", "persistent", "deferred"),
                "rationale": STRING, "evidence_ids": STRINGS}))})
SCHEMAS = {"ledger": LEDGER, "observe": OBSERVE, "decide": DECIDE,
           "materials": MATERIALS, "generate": GENERATE, "targeted": TARGETED}


def validate(value, schema, label):
    if not Draft202012Validator(schema).is_valid(value):
        # Validation messages can echo arbitrary model output. Keep logs content-free.
        raise ContractError("schema_validation_failed")


def anchor(snapshot, item):
    paragraphs = {p["id"]: p["text"] for p in snapshot["paragraphs"]}
    quote = item["quote"]
    paragraph = paragraphs.get(item["paragraph_id"], "")
    if not quote.strip() or paragraph.count(quote) != 1:
        raise ContractError("invalid_draft_anchor")


def evidence_ids(snapshot, ids):
    if set(ids) - snapshot["evidence"].keys():
        raise ContractError("unknown_evidence_id")
