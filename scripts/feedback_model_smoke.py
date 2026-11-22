"""Use two real model tasks on repository synthetic material only.

Load OPENAI_API_KEY/OPENAI_MODEL into the environment first. This does not run
new reviewers, rewrite an applicant essay, or assert human-verified quality.
"""

import argparse
import json
import os
from pathlib import Path

from admissions_editor.cloud_runtime import revision_demo_payload
from admissions_editor.editorial import build_editorial_report, demo_feedback, prepare_feedback_plan
from admissions_editor.providers import FixtureProvider, OpenAIProvider
from admissions_editor.review import run_review, sha256
from admissions_editor.revision import json_bytes, validate_revision_plan
from admissions_editor.workflow import RuleWorkflowProvider, enrich_run

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replay", type=Path, help="Validate saved synthetic model outputs offline, with no API call.")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Choose a new output directory.")
    demo = revision_demo_payload(ROOT / "examples")
    data = demo["input"]
    run = run_review(FixtureProvider(ROOT / "examples/fixtures"), data["draft"].encode(), data["requirements"],
        data["preferences"], {row["id"]: row["text"] for row in data["evidence"]})
    run = enrich_run(run, RuleWorkflowProvider(), manifest=data["claims"], source_metadata=data["sources"], as_of=data["as_of"])
    digest = sha256(json_bytes(run))
    args.out.mkdir(parents=True)

    class RecordedProvider(OpenAIProvider):
        def task(self, task, payload, schema):
            output = super().task(task, payload, schema)
            # This script is fixed to public synthetic inputs; cache responses
            # for offline diagnosis instead of repeating paid model requests.
            (args.out / (task + "-model-output.json")).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
            (args.out / "model-metrics.json").write_text(json.dumps(self.call_records, indent=2) + "\n")
            return output

    class CachedProvider:
        model = "cached_model_output"

        def __init__(self):
            self.call_records = json.loads((args.replay / "model-metrics.json").read_text())

        def task(self, task, payload, schema):
            return json.loads((args.replay / (task + "-model-output.json")).read_text())

    provider = CachedProvider() if args.replay else RecordedProvider(model=os.environ.get("OPENAI_MODEL", ""), timeout=120, retries=0)
    report = build_editorial_report(run, digest, provider)
    if report["generation_source"] != "model":
        raise RuntimeError("Real model report validation did not complete; no successful model result is claimed.")
    body = {"feedback": demo_feedback(demo), "confirmed_facts": demo["plan_input"]["confirmed_facts"], "essay_type": "sop"}
    plan = prepare_feedback_plan(run, digest, report, body, provider)
    validate_revision_plan(run, plan)
    summary = {"input_source": "repository_synthetic_revision_demo", "sample_count": 1,
        "review_prediction_source": "fixtures_and_rule", "editorial_prediction_source": "real_openai_model_cached_outputs" if args.replay else "real_openai_model",
        "model": provider.model, "model_tasks": ["editorial_report", "feedback_plan"],
        "model_api_calls": 0 if args.replay else sum(row.get("attempts", 1) for row in provider.call_records.values()),
        "source_model_api_calls": sum(row.get("attempts", 1) for row in provider.call_records.values()),
        "numbered_issue_count": len(report["issues"]), "finding_count": len(plan["findings"]),
        "complete_decision_mapping": True, "selected_skill_ids": plan["editorial_context"]["selected_skill_ids"],
        "confirmed_answer_count": len(plan["answers"]), "plan_integrity_valid": True,
        "plan_blocker_count": len(plan["blockers"]), "unresolved_question_count": len(plan["unresolved_questions"]),
        "metrics": provider.call_records,
        "scope": "Two model-task integration checks on one synthetic sample; no new essay, final verification or human quality metrics."}
    for filename, value in (("validation.json", summary), ("editorial-report.json", report), ("plan.json", plan)):
        (args.out / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
