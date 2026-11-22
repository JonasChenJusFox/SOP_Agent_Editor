import copy
import json
import unittest
from pathlib import Path

from admissions_editor.cloud_runtime import approve, demo_input, next_action, prepare_input
from admissions_editor.providers import ROLE_NAMES
from admissions_editor.review import sha256
from admissions_editor.webinput import WebError
from admissions_editor.workflow import RuleWorkflowProvider, verify_run, validated_changes

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CONFIG = {"roles": {r: {"model": "synthetic-model", "timeout": 30, "retries": 1} for r in ROLE_NAMES},
          "workflow": {"model": "synthetic-judge"}, "max_rounds": 3}


def response_for(action):
    payload = json.loads(action["payload"]["input"])
    call = action["call_id"]
    if call in ROLE_NAMES:
        value = json.loads((EXAMPLES / "fixtures" / (call + ".json")).read_text())
        for finding in value["findings"]:
            finding.setdefault("requirement_id", None)
    else:
        value = RuleWorkflowProvider().task(call, payload, {})
        if call == "decide":
            value = {"decisions": {d["group_id"]: {k: d[k] for k in ("decision", "rationale", "evidence_ids")}
                                   for d in value["decisions"]}}
    return {"response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10},
                         "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(value)}]}]}, "attempts": 1}


class CloudRuntimeTest(unittest.TestCase):
    def inputs(self):
        return prepare_input(demo_input(EXAMPLES), "demo", EXAMPLES)

    def test_offline_cloud_artifacts_and_version_bound_approval_reuse_core(self):
        result = next_action(self.inputs(), "demo", CONFIG, {}, EXAMPLES)
        self.assertTrue(result["complete"])
        verify_run(result["run"])
        changes = validated_changes(result["run"])
        self.assertTrue(changes)
        run_text = result["artifacts"]["run.json"]
        body = {"run_sha256": sha256(run_text.encode()), "draft_sha256": result["run"]["draft_sha256"],
                "change_ids": [changes[0]["id"]]}
        revision, receipt = approve(run_text, body)
        self.assertNotEqual(revision, result["run"]["snapshot"]["draft"])
        self.assertEqual(json.loads(receipt)["revised_sha256"], sha256(revision.encode()))
        body["draft_sha256"] = "stale"
        with self.assertRaises(WebError):
            approve(run_text, body)

    def test_model_calls_are_isolated_and_resume_from_exact_saved_responses(self):
        responses, actions = {}, []
        inputs = self.inputs()
        for _ in range(7):
            result = next_action(inputs, "configured", CONFIG, responses, EXAMPLES)
            if result["complete"]:
                break
            actions.append(copy.deepcopy(result))
            responses[result["key"]] = response_for(result)
        self.assertTrue(result["complete"])
        self.assertEqual([a["call_id"] for a in actions], [*ROLE_NAMES, "observe", "decide"])
        snapshots = [json.loads(a["payload"]["input"]) for a in actions[:4]]
        self.assertTrue(all(s == snapshots[0] for s in snapshots))
        self.assertEqual(set(snapshots[0]), {"draft", "paragraphs", "requirements", "preferences", "evidence", "knowledge_context"})
        knowledge = snapshots[0]["knowledge_context"]
        self.assertEqual(knowledge["status"], "missing")
        self.assertEqual(knowledge["passages"], [])
        self.assertTrue(knowledge["diagnostics"])
        observe = json.loads(actions[4]["payload"]["input"])
        self.assertEqual(set(observe), {"snapshot", "ledger"})
        decide = json.loads(actions[5]["payload"]["input"])
        self.assertNotIn("synthetic-model", json.dumps(decide))
        self.assertEqual(next_action(inputs, "configured", CONFIG, responses, EXAMPLES)["run"]["status"], "complete")
        verify_run(result["run"])

    def test_cloud_provider_partial_failure_still_reports_and_blocks_approval(self):
        responses = {}
        for _ in range(7):
            result = next_action(self.inputs(), "configured", CONFIG, responses, EXAMPLES)
            if result["complete"]:
                break
            responses[result["key"]] = ({"error": "OpenAI request failed (HTTP 429).", "attempts": 2}
                                       if result["call_id"] == "language" else response_for(result))
        self.assertEqual(result["run"]["status"], "partial")
        self.assertEqual(result["run"]["changes"], [])
        self.assertEqual(result["run"]["reviewers"]["structure"]["status"], "complete")

    def test_demo_cannot_be_used_to_claim_review_of_modified_material(self):
        raw = demo_input(EXAMPLES)
        raw["draft"] += " different text"
        with self.assertRaises(WebError):
            prepare_input(raw, "demo", EXAMPLES)


if __name__ == "__main__":
    unittest.main()
