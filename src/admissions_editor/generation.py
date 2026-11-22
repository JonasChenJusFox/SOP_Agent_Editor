"""Confirmed-material drafting. Generation metadata never enters reviewer input."""

from .contracts import MATERIALS, validate
from .review import fingerprint, paragraph_spans, sha256
from .workflow import run_task


def generate_draft(provider, materials, requirements, preferences=""):
    validate(materials, MATERIALS, "Author materials")
    if not requirements.strip():
        raise ValueError("Application requirements must contain text.")
    ids = [m["id"] for m in materials["materials"]]
    if len(set(ids)) != len(ids) or any(not i.strip() for i in ids):
        raise ValueError("Material IDs must be nonempty and unique.")
    confirmed = [m for m in materials["materials"] if m["confirmed"] and m["text"].strip()]
    if not confirmed:
        raise ValueError("At least one author-confirmed material is required.")
    questions = list(materials["questions"])
    questions += [f"请确认素材 {m['id']}" for m in materials["materials"] if not m["confirmed"]]
    # The offline composer explicitly leaves application-specific reasoning to the author.
    if provider.label.startswith("rule:"):
        questions.append("请逐项核对申请题目，并补充有官方来源支持的项目匹配理由")
    payload = {"materials": confirmed, "questions": questions,
               "requirements": requirements, "preferences": preferences}
    result, request = run_task(provider, "generate", payload)
    allowed = {m["id"] for m in confirmed}
    if not result["paragraphs"]:
        raise ValueError("The writer returned no paragraphs.")
    for paragraph in result["paragraphs"]:
        if not paragraph["text"].strip() or not paragraph["material_ids"] or set(paragraph["material_ids"]) - allowed:
            raise ValueError("Every generated paragraph must cite confirmed materials only.")
        if len(paragraph_spans(paragraph["text"])) != 1 or "\x00" in paragraph["text"]:
            raise ValueError("Each writer paragraph must be a single plain-text paragraph.")
    # Missing author questions cannot be silently dropped by a writer.
    missing = list(dict.fromkeys(questions + result["missing_information"]))
    draft = "\n\n".join(p["text"] for p in result["paragraphs"])
    if missing:
        draft += "\n\n" + "\n".join(f"[待补: {question}]" for question in missing)
    draft += "\n"
    return draft, {"schema_version": 1, "mode": provider.label, "request": request,
                   "materials_sha256": fingerprint(materials), "requirements_sha256": sha256(requirements.encode()),
                   "paragraph_material_map": [{"paragraph_id": f"P{i:03d}", **p}
                                              for i, p in enumerate(result["paragraphs"], 1)],
                   "missing_information": missing, "draft_sha256": sha256(draft.encode()),
                   "needs_author_fact_check": True}
