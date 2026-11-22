"""Failure messages are useful without exposing model or author exception text."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from admissions_editor.revision_failure import (RevisionFailure, failure_diagnostic,
    failed_revision_result, validate_failure_diagnostic)
import test_revision_api as helpers
from test_editorial_api import body_for

ROOT = Path(__file__).resolve().parents[1]


class FailureDiagnosticTest(unittest.TestCase):
    def test_value_error_has_actionable_bounded_message_without_exception_text(self):
        diagnostic = failure_diagnostic(ValueError("DO-NOT-LOG-PRIVATE-TEXT"), "feedback_plan")
        self.assertEqual(diagnostic["code"], "feedback_interpretation_invalid")
        self.assertEqual(diagnostic["stage"], "feedback_plan")
        self.assertIn("无需重新审稿", diagnostic["user_message"])
        self.assertNotIn("DO-NOT-LOG", json.dumps(diagnostic))
        self.assertEqual(diagnostic["error_type"], "ValueError")

    def test_structured_codes_select_only_fixed_public_messages(self):
        diagnostic = failure_diagnostic(RevisionFailure("feedback_excerpt_invalid", "feedback_plan"))
        self.assertEqual(diagnostic["code"], "feedback_excerpt_invalid")
        malicious = {**diagnostic, "user_message": "DO-NOT-LOG-PRIVATE-TEXT", "error_type": "Bad value SECRET"}
        validated = validate_failure_diagnostic(malicious)
        self.assertNotIn("SECRET", json.dumps(validated))
        self.assertNotIn("DO-NOT-LOG", json.dumps(validated))
        self.assertEqual(validate_failure_diagnostic({"schema_version": "unknown"})["code"], "revision_failed")

    def test_known_validation_constants_map_without_copying_unknown_exception_prose(self):
        diagnostic = failure_diagnostic(ValueError("A confirmed answer must quote an explicit assertion in the author's feedback."), "feedback_plan")
        self.assertEqual(diagnostic["code"], "feedback_excerpt_invalid")
        unknown = failure_diagnostic(ValueError("A confirmed answer contains PRIVATE-AUTHOR-TEXT"), "feedback_plan")
        self.assertEqual(unknown["code"], "feedback_interpretation_invalid")
        self.assertNotIn("PRIVATE-AUTHOR", json.dumps(unknown))

    def test_recovered_options_are_filtered_copied_and_never_mark_final(self):
        inputs = {"feedback_request": {"feedback": "Keep the synthetic opening.", "essay_type": "sop",
            "word_limit": 500, "writing_sample": "Synthetic voice", "glossary": [], "confirmed_facts": [],
            "unexpected_private_debug": "NOT-AN-AUTHOR-OPTION"}, "editorial_report": {"report_sha256": "b" * 64}}
        result = failed_revision_result(inputs, "synthetic-review", "a" * 64)
        self.assertEqual(result["feedback"], inputs["feedback_request"]["feedback"])
        self.assertEqual(result["feedback_options"]["word_limit"], 500)
        self.assertNotIn("unexpected_private_debug", result["feedback_options"])
        self.assertFalse(result["validation"]["passed"])
        self.assertNotIn("candidate_text", result)
        self.assertNotIn("final_text", result)
        result["feedback_options"]["confirmed_facts"].append({"changed": True})
        self.assertEqual(inputs["feedback_request"]["confirmed_facts"], [])


class LocalFailureApiTest(unittest.TestCase):
    setUp = helpers.LocalRevisionApiTest.setUp
    close = helpers.LocalRevisionApiTest.close
    request = helpers.LocalRevisionApiTest.request
    json_request = helpers.LocalRevisionApiTest.json_request
    completed_job = helpers.LocalRevisionApiTest.completed_job
    completed_review = helpers.LocalRevisionApiTest.completed_review

    def test_feedback_failure_persists_safe_diagnostic_and_restores_original_comment(self):
        demo, review = self.completed_review()
        body = body_for(demo, review)
        with patch("admissions_editor.webserver.prepare_feedback_plan", side_effect=ValueError("DO-NOT-LOG-PRIVATE-TEXT")):
            submitted = self.json_request(f"/api/reviews/{review['id']}/feedback-revisions", body, 202)
            self.app.executor.shutdown(wait=True)
        response = self.json_request(f"/api/revisions/{submitted['id']}")
        self.assertEqual(response["state"], "failed")
        result = response["result"]
        self.assertEqual(result["diagnostic"]["stage"], "feedback_plan")
        self.assertEqual(result["diagnostic"]["code"], "feedback_interpretation_invalid")
        self.assertEqual(result["feedback"], body["feedback"])
        self.assertEqual(result["feedback_options"]["confirmed_facts"], body["confirmed_facts"])
        self.assertEqual(result["source_review_id"], review["id"])
        self.assertNotIn("DO-NOT-LOG", json.dumps(response))
        diagnostic = json.loads((self.app.root / submitted["id"] / "internal/revision-failure.json").read_text())
        self.assertEqual(diagnostic, result["diagnostic"])
        self.app.password = "synthetic-diagnostic-password"
        self.assertEqual(self.request(f"/api/revisions/{submitted['id']}")[0], 401)


class CloudFailureApiTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.CloudRevisionApiTest.asyncSetUp
    asyncTearDown = helpers.CloudRevisionApiTest.asyncTearDown
    call = helpers.CloudRevisionApiTest.call
    json_call = helpers.CloudRevisionApiTest.json_call
    completed_review = helpers.CloudRevisionApiTest.completed_review

    async def test_actual_cloud_host_catches_validation_error_and_persists_only_safe_diagnostic(self):
        demo, review = await self.completed_review()
        body = body_for(demo, review)
        submitted = await self.json_call(f"/api/reviews/{review['id']}/feedback-revisions", "POST", body, 202)
        row = await self.app.store.job(submitted["id"])
        # Import the real native-host dispatcher with only its platform base
        # classes substituted. This executes the actual exception persistence.
        platform = SimpleNamespace(Response=object, WorkerEntrypoint=object, WorkflowEntrypoint=object, fetch=None)
        spec = importlib.util.spec_from_file_location("synthetic_revision_host", ROOT / "cloudflare/python/entry.py")
        module = importlib.util.module_from_spec(spec)
        original_read = Path.read_text

        def read_settings(path, *args, **kwargs):
            return "{}" if path.name == "settings.json" else original_read(path, *args, **kwargs)

        with patch.dict(sys.modules, {"workers": platform}), patch.object(Path, "read_text", read_settings):
            spec.loader.exec_module(module)
        with patch.object(module, "next_feedback_revision_action", side_effect=ValueError("DO-NOT-LOG-PRIVATE-TEXT")):
            outcome = await module.ReviewWorkflow().run_revision(row, SimpleNamespace(), self.app.store)
        self.assertEqual(outcome, {"state": "failed"})
        response = await self.json_call(f"/api/revisions/{submitted['id']}")
        self.assertEqual(response["result"]["diagnostic"]["code"], "feedback_interpretation_invalid")
        self.assertEqual(response["result"]["diagnostic"]["stage"], "feedback_plan")
        saved = await self.app.store.read(submitted["id"], "internal/revision-failure.json")
        self.assertNotIn("DO-NOT-LOG", saved)
        self.assertEqual(response["result"]["feedback"], body["feedback"])

    async def test_saved_failure_is_private_actionable_and_restores_frozen_author_inputs(self):
        demo, review = await self.completed_review()
        body = body_for(demo, review)
        submitted = await self.json_call(f"/api/reviews/{review['id']}/feedback-revisions", "POST", body, 202)
        diagnostic = failure_diagnostic(ValueError("DO-NOT-LOG-PRIVATE-TEXT"), "feedback_plan")
        await self.app.store.write(submitted["id"], {"internal/revision-failure.json": json.dumps(diagnostic)},
            ("failed", diagnostic["user_message"]))
        response = await self.json_call(f"/api/revisions/{submitted['id']}")
        self.assertEqual(response["diagnostic"], diagnostic)
        self.assertEqual(response["result"]["feedback"], body["feedback"])
        self.assertEqual(response["result"]["feedback_options"]["confirmed_facts"], body["confirmed_facts"])
        self.assertEqual(response["result"]["source_review_id"], review["id"])
        self.assertFalse(response["result"]["validation"]["passed"])
        self.assertNotIn("DO-NOT-LOG", json.dumps(response))
        self.assertEqual((await self.call(f"/api/revisions/{submitted['id']}", cookie=False)).status, 401)

    async def test_historical_failure_without_diagnostic_recovers_comment_without_guessing_cause(self):
        demo, review = await self.completed_review()
        body = body_for(demo, review)
        submitted = await self.json_call(f"/api/reviews/{review['id']}/feedback-revisions", "POST", body, 202)
        await self.app.store.update(submitted["id"], "failed", "Legacy failed run")
        response = await self.json_call(f"/api/revisions/{submitted['id']}")
        self.assertEqual(response["result"]["diagnostic"]["code"], "revision_failed")
        self.assertEqual(response["result"]["diagnostic"]["stage"], "unknown")
        self.assertEqual(response["result"]["feedback"], body["feedback"])
        self.assertEqual(response["result"]["run_sha256"], body["run_sha256"])


if __name__ == "__main__":
    unittest.main()
