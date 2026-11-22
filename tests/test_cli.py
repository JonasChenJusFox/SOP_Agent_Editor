import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("structure", "admissions", "language", "skeptical")
DRAFT = "I built the the prototype for a weekend project.\n\nThe work led me to study software design.\n"
BEFORE = "the the prototype"
AFTER = "the prototype"


class EditorialCliTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.draft = self.workspace / "draft.txt"
        self.draft.write_text(DRAFT, encoding="utf-8")
        self.requirements = self.workspace / "requirements.txt"
        self.requirements.write_text("Describe a project and explain your motivation.\n", encoding="utf-8")
        self.fixtures = self.workspace / "fixtures"
        self.fixtures.mkdir()
        self.language_finding = {
            "kind": "issue",
            "category": "language",
            "severity": "low",
            "paragraph_id": "P001",
            "quote": BEFORE,
            "rationale": "An article is duplicated within the noun phrase.",
            "action": "revise",
            "replacement": AFTER,
            "evidence_ids": [],
        }
        for role in ROLES:
            findings = [self.language_finding] if role == "language" else []
            self.write_fixture(role, findings)
        self.output = self.workspace / "revised.txt"

    def write_fixture(self, role, findings):
        (self.fixtures / f"{role}.json").write_text(
            json.dumps({"role": role, "findings": findings}),
            encoding="utf-8",
        )

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "admissions_editor", *map(str, arguments)],
            cwd=self.workspace,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=15,
        )

    def review(self, *, directory="run", expected_exit=0):
        run = self.workspace / directory
        result = self.cli(
            "review",
            "--draft", self.draft,
            "--requirements", self.requirements,
            "--fixtures", self.fixtures,
            "--out", run,
        )
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        return run, state

    def approve(self, run, change_id):
        return self.cli(
            "approve",
            "--run", run,
            "--draft", self.draft,
            "--change", change_id,
            "--out", self.output,
        )

    def assert_approval_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".txt.approval.json").exists())

    def test_schema_is_available_without_provider_configuration(self):
        result = self.cli("schema")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        schema = json.loads(result.stdout)
        self.assertEqual(set(schema["required"]), {"role", "findings"})
        self.assertEqual(set(schema["properties"]["role"]["enum"]), set(ROLES))

    def test_review_and_approval_write_a_separate_revision(self):
        run, state = self.review()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(set(state["reviewers"]), set(ROLES))
        self.assertEqual(state["draft_sha256"], hashlib.sha256(DRAFT.encode()).hexdigest())
        self.assertEqual(len(state["changes"]), 1)
        change, = state["changes"]
        self.assertEqual((change["quote"], change["replacement"]), (BEFORE, AFTER))
        self.assertIn(change["id"], (run / "report.md").read_text(encoding="utf-8"))
        diff = (run / "proposed.diff").read_text(encoding="utf-8")
        self.assertIn("-I built the the prototype", diff)
        self.assertIn("+I built the prototype", diff)
        self.assertFalse(self.output.exists())

        approved = self.approve(run, change["id"])
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertEqual(self.draft.read_bytes(), DRAFT.encode())
        self.assertEqual(self.output.read_bytes(), DRAFT.replace(BEFORE, AFTER).encode())
        receipt = self.output.with_suffix(".txt.approval.json")
        self.assertIsInstance(json.loads(receipt.read_text(encoding="utf-8")), dict)

    def test_approval_rejects_stale_draft_even_if_selected_span_is_unchanged(self):
        run, state = self.review()
        updated = DRAFT + "\nI later tested it.\n"
        self.draft.write_text(updated, encoding="utf-8")
        self.assert_approval_failed(self.approve(run, state["changes"][0]["id"]))
        self.assertEqual(self.draft.read_bytes(), updated.encode())

    def test_missing_reviewer_reports_partial_run_and_blocks_approval(self):
        _, complete = self.review(directory="complete")
        (self.fixtures / "skeptical.json").unlink()
        run, state = self.review(expected_exit=2)
        self.assertEqual(state["status"], "partial")
        self.assertIn("skeptical", (run / "report.md").read_text(encoding="utf-8").lower())
        self.assert_approval_failed(self.approve(run, complete["changes"][0]["id"]))

    def test_overlapping_preserve_finding_withholds_edit(self):
        _, baseline = self.review(directory="baseline")
        self.write_fixture("structure", [{
            "kind": "preserve",
            "category": "structure",
            "severity": "low",
            "paragraph_id": "P001",
            "quote": "I built the the prototype",
            "rationale": "The phrase establishes the author's ownership of the project.",
            "action": "preserve",
            "replacement": None,
            "evidence_ids": [],
        }])
        run, state = self.review()
        self.assertEqual(state["status"], "complete")
        self.assertFalse(state["changes"])
        self.assert_approval_failed(self.approve(run, baseline["changes"][0]["id"]))

    def test_fabricated_quote_cannot_become_a_change(self):
        fabricated_quote = "a sentence that never existed"
        self.write_fixture("language", [{**self.language_finding, "quote": fabricated_quote}])
        run, state = self.review(expected_exit=2)
        self.assertEqual(state["status"], "partial")
        self.assertFalse(state["changes"])
        self.assertNotIn(fabricated_quote, (run / "proposed.diff").read_text(encoding="utf-8"))

    def test_unknown_evidence_id_and_extra_schema_field_disable_approval(self):
        self.write_fixture("language", [{**self.language_finding, "evidence_ids": ["unknown.txt"]}])
        _, state = self.review(expected_exit=2)
        self.assertFalse(state["changes"])
        self.write_fixture("language", [{**self.language_finding, "unexpected": "field"}])
        _, state = self.review(directory="schema-failure", expected_exit=2)
        self.assertEqual(state["reviewers"]["language"]["status"], "failed")
        self.write_fixture("language", [{**self.language_finding, "requirement_id": "MADE_UP_REQUIREMENT"}])
        _, state = self.review(directory="unknown-requirement", expected_exit=2)
        self.assertEqual(state["reviewers"]["language"]["status"], "degraded")

    def test_numeric_or_content_changing_replacement_is_visible_but_cannot_be_approved(self):
        for index, replacement in enumerate(("the prototype for 100 users", "the entire ranking engine")):
            self.write_fixture("language", [{**self.language_finding, "replacement": replacement}])
            run, state = self.review(directory=f"unsafe-{index}")
            self.assertFalse(state["changes"])
            details = json.loads((run / "candidate-details.json").read_text())["items"]
            self.assertEqual(len(details), 1)
            self.assertFalse(details[0]["eligible"])
            self.assertTrue((run / "changes/language-01.diff").exists())

    def test_duplicate_selection_and_reusing_output_are_rejected(self):
        run, state = self.review()
        change = state["changes"][0]["id"]
        result = self.cli("approve", "--run", run, "--draft", self.draft,
                          "--change", change, "--change", change, "--out", self.output)
        self.assert_approval_failed(result)
        self.assertEqual(self.approve(run, change).returncode, 0)
        original = self.output.read_bytes()
        self.assertNotEqual(self.approve(run, change).returncode, 0)
        self.assertEqual(self.output.read_bytes(), original)

    def test_approval_cannot_overwrite_original_or_apply_unknown_id(self):
        run, state = self.review()
        result = self.cli("approve", "--run", run, "--draft", self.draft,
                          "--change", state["changes"][0]["id"], "--out", self.draft)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.draft.read_bytes(), DRAFT.encode())
        self.assert_approval_failed(self.approve(run, "CHG-unavailable"))


if __name__ == "__main__":
    unittest.main()
