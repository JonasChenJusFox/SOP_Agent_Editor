"""Rebuild synthetic evaluation fixtures. No private material or API is used."""

import json
from pathlib import Path

from admissions_editor.review import fingerprint, sha256

ROOT = Path(__file__).resolve().parents[1] / "examples" / "evaluation"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build():
    cases, rows = [], []
    for family in range(8):
        kind = family % 4
        category = ["evidence", "evidence", "admissions", "language"][kind]
        severity = "low" if kind == 3 else "high"
        bad = ["I alone designed the ranking engine.", "Our prototype improved accuracy by 30% on all tasks.",
               "I want graduate training.", "I built the the prototype."][kind]
        good = ["Our team designed the ranking engine.", "Our team tested the prototype on 20 saved examples.",
                "The program's verified systems course fits my goal of studying fault tolerance.", "I built the prototype."][kind]
        for problem in (True, False):
            case_id = f"SYN-{family + 1:02d}-{'issue' if problem else 'clean'}"
            target = bad if problem else good
            draft = f"I explored synthetic project {family + 1}.\n\n{target}\n\nThat question shaped my interest in reliable systems.\n"
            missing = kind == 2 and problem
            case = {"case_id": case_id, "family_id": f"FAMILY-{family + 1:02d}", "split": "test",
                    "origin": "synthetic", "draft": draft, "document_sha256": sha256(draft.encode()),
                    "requirements": "Describe your contribution. Explain why this program fits your goals. Requirement WHY_PROGRAM: give a verified program resource.",
                    "preferences": "Keep the final reflection sentence unchanged.",
                    "evidence": {"notes.txt": "The ranking engine was team work. The team tested 20 saved examples; no accuracy improvement was measured. The synthetic program offers a systems course."},
                    "writer_model": "A" if family < 4 else "B", "writer_provenance": "synthetic_assignment",
                    "annotation": {"version": "synthetic-gold-1", "annotators": [], "adjudicator": None,
                                   "status": "synthetic", "consent_reference": None},
                    "units": [{"id": "target", "label": "issue" if problem else "clean", "category": category,
                               "severity": severity, "paragraph_id": None if missing else "P002", "quote": None if missing else target,
                               "requirement_id": "WHY_PROGRAM" if missing else None, "evidence_ids": ["notes.txt"],
                               "rationale": "Synthetic injected defect or its paired clean control.",
                               "acceptable_outcomes": ["verify", "revise"] if problem else ["preserve", "no_alert"]},
                              {"id": "voice", "label": "clean", "category": "language", "severity": "low", "paragraph_id": "P003",
                               "quote": "That question shaped my interest in reliable systems.", "requirement_id": None,
                               "evidence_ids": [], "rationale": "Protected, clear author reflection.", "acceptable_outcomes": ["preserve", "no_alert"]}]}
            cases.append(case)
            for role in ("structure", "admissions", "language", "skeptical"):
                chosen = "language" if category == "language" else "admissions" if category == "admissions" else "skeptical"
                findings = []
                if problem and role == chosen:
                    findings = [{"kind": "question" if category == "evidence" else "issue", "category": category,
                                 "severity": severity, "paragraph_id": "P002", "quote": target,
                                 "rationale": "The synthetic evidence or explicit requirement does not support this passage.",
                                 "action": "verify" if category == "evidence" else "revise",
                                 "replacement": good if category == "language" else None,
                                 "requirement_id": "WHY_PROGRAM" if missing else None,
                                 "evidence_ids": ["notes.txt"] if category == "evidence" else []}]
                write(ROOT / "fixtures" / case_id / f"{role}.json", {"role": role, "findings": findings})
            for model in ("A", "B"):
                same = model == case["writer_model"]
                # Explicitly artificial behavior to exercise bias arithmetic, not model performance.
                miss = problem and same and kind in (0, 1)
                failed = family == 7 and problem and not same
                findings = []
                if problem and not miss and not failed:
                    findings.append({"unit_id": "target", "category": category, "severity": severity,
                                     "rationale_valid": True, "confidence": .8})
                if not problem and family == 0 and model == "B":
                    findings.append({"unit_id": "voice", "category": "language", "severity": "low",
                                     "rationale_valid": False, "confidence": .6})
                rows.append({"case_id": case_id, "document_sha256": case["document_sha256"], "reviewer_model": model,
                             "role": "report", "arm": "four", "source": "synthetic_fixture",
                             "status": "failed" if failed else "complete", "assessed_unit_ids": [] if failed else ["target", "voice"],
                             "findings": findings, "call_count": 4, "latency_seconds": 0,
                             "input_tokens": 0, "output_tokens": 0, "expected_roles": 4, "successful_roles": 0 if failed else 4})
    dataset = {"version": "synthetic-1", "cases": cases}
    write(ROOT / "dataset.json", dataset)
    write(ROOT / "predictions.json", {"version": "predictions-1", "dataset_sha256": fingerprint(dataset), "runs": rows})
    print(f"Wrote {len(cases)} synthetic cases, 8 families, {len(rows)} prescribed runs.")


if __name__ == "__main__":
    build()
