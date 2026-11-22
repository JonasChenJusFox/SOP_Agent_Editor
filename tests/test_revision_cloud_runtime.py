"""Durable replay of the full writing pipeline without network access."""

import copy
import json
import unittest
from pathlib import Path

from admissions_editor.cloud_runtime import next_action, next_revision_action, prepare_input, revision_demo_payload
from admissions_editor.revision import prepare_revision_plan, verify_revision
from admissions_editor.review import sha256

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STAGES = ("sop_revise", "polish", "humanize", "verify_revision")


class RevisionReplayTest(unittest.TestCase):
    def setUp(self):
        manifest = revision_demo_payload(EXAMPLES)
        source = next_action(prepare_input(manifest["input"], "demo", EXAMPLES), "demo", {}, {}, EXAMPLES)
        self.run = source["run"]
        self.plan = prepare_revision_plan(self.run, sha256(source["artifacts"]["run.json"].encode()), **manifest["plan_input"])
        self.config = {"revision": {stage: {"model": "synthetic-" + stage, "timeout": 30, "retries": 0} for stage in STAGES}}

    def test_four_durable_calls_replay_exactly_and_verifier_is_last(self):
        responses, actions = {}, []
        for _ in range(5):
            action = next_revision_action(self.run, self.plan, self.config, responses, EXAMPLES)
            if action["complete"]:
                break
            actions.append(copy.deepcopy(action))
            repeated = next_revision_action(self.run, self.plan, self.config, responses, EXAMPLES)
            self.assertEqual(action, repeated)
            value = json.loads((EXAMPLES / "revision/fixtures" / (action["call_id"] + ".json")).read_text())
            responses[action["key"]] = {"response": {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": json.dumps(value)}]}]}, "attempts": 1, "elapsed_seconds": 0.1}
        self.assertTrue(action["complete"])
        self.assertEqual([a["call_id"] for a in actions], list(STAGES))
        self.assertEqual([a["payload"]["model"] for a in actions], ["synthetic-" + stage for stage in STAGES])
        self.assertEqual(action["record"]["status"], "ready")
        self.assertIn("final-essay.txt", action["artifacts"])
        self.assertTrue(verify_revision(action["record"]))
        self.assertEqual(action["record"]["candidate"], (EXAMPLES / "revision/expected-final.txt").read_text())
        self.assertTrue(next_revision_action(self.run, self.plan, self.config, responses, EXAMPLES)["complete"])

    def test_failed_later_response_retains_candidate_without_ready_final(self):
        responses = {}
        for _ in range(3):
            action = next_revision_action(self.run, self.plan, self.config, responses, EXAMPLES)
            if action["complete"]:
                break
            if action["call_id"] == "polish":
                responses[action["key"]] = {"error": "OpenAI request failed (HTTP 429).", "attempts": 1}
            else:
                value = json.loads((EXAMPLES / "revision/fixtures/sop_revise.json").read_text())
                responses[action["key"]] = {"response": {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(value)}]}]}}
        self.assertTrue(action["complete"])
        self.assertEqual(action["record"]["status"], "failed")
        self.assertTrue(action["record"]["candidate"])
        self.assertNotIn("final-essay.txt", action["artifacts"])


if __name__ == "__main__":
    unittest.main()
