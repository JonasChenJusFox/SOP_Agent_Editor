"""Old candidates stay readable after a policy upgrade, never approvable."""

import copy
import unittest

from admissions_editor.review import fingerprint, sha256
from admissions_editor.revision import json_bytes, prepare_revision_plan, revise_document
from admissions_editor.revision_web import finalize_web, revision_result
from admissions_editor.webinput import WebError
from test_revision import Editor, make_source


def seal(value, field):
    value[field] = fingerprint({key: item for key, item in value.items() if key != field})


class LegacyRevisionViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run, options = make_source()
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), **options)
        cls.current = revise_document(run, plan, Editor())

    def historical(self):
        # Structural fixture for the read-only adapter, not a claim that v4
        # stage requests were generated under the historical prompt policy.
        record = copy.deepcopy(self.current)
        record["schema_version"] = record["plan"]["schema_version"] = "full-revision-3"
        seal(record["plan"], "plan_sha256")
        seal(record, "record_sha256")
        return record

    def test_historical_ready_candidate_preserves_author_inputs_but_cannot_release(self):
        record = self.historical()
        original = copy.deepcopy(record)
        result = revision_result(record, "synthetic-review", ["candidate.txt", "revision.json"])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["legacy_read_only"])
        self.assertTrue(result["requires_replan"])
        self.assertFalse(result["validation"]["passed"])
        self.assertNotIn("final_text", result)
        self.assertEqual(result["plan"]["confirmed_facts"], record["plan"]["confirmed_facts"])
        self.assertEqual(result["plan"]["answers"], record["plan"]["answers"])
        self.assertEqual(result["candidate_text"], record["candidate"])
        self.assertEqual(record, original)
        with self.assertRaises(WebError) as caught:
            finalize_web(record, {"candidate_sha256": record["candidate_sha256"],
                                  "plan_sha256": record["plan"]["plan_sha256"]})
        self.assertEqual(caught.exception.status, 409)

    def test_historical_tampering_without_seals_is_rejected(self):
        for target in ("candidate", "plan"):
            record = self.historical()
            if target == "candidate":
                record["candidate"] += " Tampered."
            else:
                record["plan"]["confirmed_facts"][0]["text"] += " Tampered."
            with self.subTest(target=target), self.assertRaises(WebError) as caught:
                revision_result(record, "synthetic-review", [])
            self.assertEqual(caught.exception.status, 409)

    def test_resealing_does_not_hide_changed_candidate_or_source_binding(self):
        for target in ("candidate", "source", "diff"):
            record = self.historical()
            if target == "candidate":
                record["candidate"] += " Changed."
            elif target == "source":
                record["plan"]["draft_sha256"] = "0" * 64
                seal(record["plan"], "plan_sha256")
            else:
                record["full_diff"] = ""
            seal(record, "record_sha256")
            with self.subTest(target=target), self.assertRaises(WebError) as caught:
                revision_result(record, "synthetic-review", [])
            self.assertEqual(caught.exception.status, 409)

    def test_unknown_historical_version_is_rejected(self):
        record = self.historical()
        record["schema_version"] = record["plan"]["schema_version"] = "full-revision-2"
        seal(record["plan"], "plan_sha256")
        seal(record, "record_sha256")
        with self.assertRaises(WebError) as caught:
            revision_result(record, "synthetic-review", [])
        self.assertEqual(caught.exception.status, 409)

    def test_current_verified_candidate_still_requires_exact_acceptance(self):
        result = revision_result(copy.deepcopy(self.current), "synthetic-review", [])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["final_text"], self.current["candidate"])
        self.assertNotIn("requires_replan", result)
        with self.assertRaises(WebError) as caught:
            finalize_web(self.current, {"candidate_sha256": "0" * 64,
                                        "plan_sha256": self.current["plan"]["plan_sha256"]})
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
