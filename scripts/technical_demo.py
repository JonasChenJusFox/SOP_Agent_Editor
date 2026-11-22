"""Reproduce technical-meaning regressions with synthetic text and no API calls."""

import argparse
import json
from pathlib import Path

from admissions_editor.cli import publish_directory
from admissions_editor.revision import json_bytes
from admissions_editor.technical import check_technical_accuracy, extract_technical_context

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "examples/technical/cases.json")
    parser.add_argument("--out", type=Path, default=Path("runs/technical-demo"))
    args = parser.parse_args()
    manifest = json.loads(args.cases.read_text())
    if manifest.get("input_source") != "synthetic":
        raise ValueError("This demonstration accepts explicitly synthetic cases only.")
    artifacts, results = {}, []
    for case in manifest["cases"]:
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid case ID")
        context = extract_technical_context({"draft": case["draft"], "evidence": case.get("evidence", {})}, case.get("glossary"))
        report = check_technical_accuracy(context, case["candidate"])
        kinds = {issue["kind"] for issue in report["issues"]}
        assert report["passed"] is case["expected_pass"], (case_id, report)
        assert set(case.get("expected_issue_kinds", [])).issubset(kinds), (case_id, kinds)
        artifacts[f"{case_id}/technical-context.json"] = json_bytes(context)
        artifacts[f"{case_id}/technical-accuracy.json"] = json_bytes(report)
        artifacts[f"{case_id}/candidate.txt"] = case["candidate"].encode()
        results.append({"case_id": case_id, "expected_pass": case["expected_pass"],
                        "actual_pass": report["passed"], "issue_kinds": sorted(kinds)})
    summary = {"input_source": "synthetic", "prediction_source": "offline_lexical_and_source_checks",
               "sample_count": len(results), "model_api_calls": 0, "cases": results,
               "scope": "Deterministic behavior tests; not real-model or human semantic accuracy metrics."}
    artifacts["validation.json"] = json_bytes(summary)
    publish_directory(args.out, artifacts)
    print(f"Passed {len(results)} synthetic technical scenarios; model_api_calls=0: {args.out / 'validation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
