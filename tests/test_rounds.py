import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from admissions_editor.cli import main
from admissions_editor.providers import ROLE_NAMES
from admissions_editor.review import run_review
from admissions_editor.workflow import RuleWorkflowProvider, enrich_run


class RoundCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.draft = self.root / "draft.txt"
        self.draft.write_text("I built the the prototype.")
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("Describe a project.")
        self.fixtures = self.root / "fixtures"
        self.fixtures.mkdir()
        for role in ROLE_NAMES:
            self.write_fixture(role, [])
        self.write_fixture("language", [{"kind": "issue", "category": "language", "severity": "low",
            "paragraph_id": "P001", "quote": "the the prototype", "rationale": "Repeated article.",
            "action": "revise", "replacement": "the prototype", "evidence_ids": []}])

    def write_fixture(self, role, findings):
        (self.fixtures / f"{role}.json").write_text(json.dumps({"role": role, "findings": findings}))

    def cli(self, *args, expected=0):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main([str(a) for a in args])
        self.assertEqual(code, expected, out.getvalue() + err.getvalue())

    def review(self, name="first", draft=None, extra=(), expected=0):
        out = self.root / name
        self.cli("review", "--draft", draft or self.draft, "--requirements", self.requirements,
                 "--fixtures", self.fixtures, "--out", out, *extra, expected=expected)
        return out, json.loads((out / "run.json").read_text()) if out.exists() else None

    def approved(self, run, state, keep=False):
        revised = self.root / "revised.txt"
        selected = ["--keep"] if keep else ["--change", state["changes"][0]["id"]]
        self.cli("approve", "--run", run, "--draft", self.draft, *selected, "--out", revised)
        return revised, revised.with_name("revised.txt.approval.json")

    def test_followup_links_receipt_and_closes_only_the_exact_approved_local_issue(self):
        first, state = self.review()
        revised, receipt = self.approved(first, state)
        self.write_fixture("language", [])
        _, second = self.review("second", revised, ["--parent-run", first, "--approval-receipt", receipt])
        self.assertEqual(second["round"]["number"], 2)
        self.assertIsNotNone(second["round"]["parent_run_sha256"])
        self.assertEqual(second["issues"][0]["issue_id"], state["issues"][0]["issue_id"])
        self.assertEqual(second["issues"][0]["status"], "resolved")
        self.assertEqual(second["issues"][0]["resolution_basis"], "approved_local_language_change")
        self.assertEqual(second["targeted_review"]["status"], "complete")

    def test_edited_revision_or_wrong_receipt_cannot_claim_parent_approval(self):
        first, state = self.review()
        revised, receipt = self.approved(first, state)
        revised.write_text(revised.read_text() + " Changed after approval.")
        out, _ = self.review("invalid", revised, ["--parent-run", first, "--approval-receipt", receipt], expected=1)
        self.assertFalse(out.exists())

    def test_round_limit_is_checked_before_producing_another_run(self):
        first, state = self.review(extra=["--max-rounds", 1])
        revised, receipt = self.approved(first, state)
        out, _ = self.review("limited", revised, ["--parent-run", first, "--approval-receipt", receipt], expected=1)
        self.assertFalse(out.exists())

    def test_noop_approval_supports_persistent_issue_tracking(self):
        first, state = self.review()
        revised, receipt = self.approved(first, state, keep=True)
        self.assertEqual(revised.read_bytes(), self.draft.read_bytes())
        _, second = self.review("second", revised, ["--parent-run", first, "--approval-receipt", receipt])
        self.assertEqual(second["issues"][0]["status"], "persistent")

    def test_author_closed_issue_that_reappears_is_tracked_as_recurring(self):
        first, state = self.review()
        revised, receipt = self.approved(first, state, keep=True)
        feedback = self.root / "feedback.json"
        self.cli("feedback", "--run", first, "--issue", state["issues"][0]["issue_id"], "--decision", "resolved",
                 "--reason", "Synthetic author resolution claim.", "--out", feedback)
        _, second = self.review("second", revised, ["--parent-run", first, "--approval-receipt", receipt, "--feedback", feedback])
        self.assertEqual(second["issues"][0]["status"], "recurring")
        self.assertEqual(second["round"]["stop_reason"], "pause_recurring_issues")

    def test_targeted_review_failure_is_partial_and_blocks_new_changes(self):
        first, parent = self.review()

        class FailsTargeted(RuleWorkflowProvider):
            def task(self, task, payload, schema):
                if task == "targeted":
                    raise ValueError("synthetic failure")
                return super().task(task, payload, schema)

        class EmptyReviewer:
            label = "fixture"

            def review(self, role, payload):
                return {"role": role, "findings": []}

        run = enrich_run(run_review(EmptyReviewer(), b"A reflection.", "Describe a project.", "", {}),
                         FailsTargeted(), parent=parent, as_of="2026-11-18")
        self.assertEqual(run["status"], "partial")
        self.assertFalse(run["changes"])
        self.assertEqual(run["issues"][0]["status"], "deferred")

    def failed_adjudication(self):
        workflows = self.root / "workflow-fixtures"
        workflows.mkdir()
        (workflows / "observe.json").write_text(json.dumps({"observations": [
            {"paragraph_id": "P001", "quote": self.draft.read_text(), "assessment": "Synthetic unverified claim.",
             "evidence_ids": ["AUTO-claim-used-as-source"]}]}))
        config = self.root / "bad-workflow.toml"
        config.write_text('[workflow]\nprovider="fixture"\nfixtures="workflow-fixtures"\n')
        return self.review(extra=["--config", config], expected=2)

    def test_retry_cli_recovers_failed_adjudication_then_allows_normal_approval(self):
        source, state = self.failed_adjudication()
        original_bytes = (source / "run.json").read_bytes()
        out = self.root / "recovered"
        # Missing reviewer fixtures prove the retry reads cached reviewer outputs.
        for path in self.fixtures.iterdir():
            path.unlink()
        self.cli("retry-adjudication", "--run", source, "--draft", self.draft, "--out", out)
        recovered = json.loads((out / "run.json").read_text())
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["recovery"]["new_reviewer_calls"], 0)
        self.assertEqual((source / "run.json").read_bytes(), original_bytes)
        self.approved(out, recovered)

    def test_retry_cli_rejects_a_stale_draft_before_running_workflow(self):
        source, _ = self.failed_adjudication()
        self.draft.write_text(self.draft.read_text() + " Later author edit.")
        out = self.root / "stale-retry"
        self.cli("retry-adjudication", "--run", source, "--draft", self.draft, "--out", out, expected=1)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
