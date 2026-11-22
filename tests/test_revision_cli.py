import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from admissions_editor.cli import main

ROOT = Path(__file__).resolve().parents[1]


class RevisionCliTest(unittest.TestCase):
    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, stdout.getvalue(), stderr.getvalue()

    def source(self, folder):
        run = folder / "review"
        draft = folder / "source.txt"
        draft.write_bytes((ROOT / "examples/draft.txt").read_bytes())
        code, _, error = self.call("review", "--draft", draft, "--requirements", ROOT / "examples/requirements.txt",
            "--preferences", ROOT / "examples/preferences.txt", "--fixtures", ROOT / "examples/fixtures",
            "--evidence-dir", ROOT / "examples/revision/evidence", "--out", run)
        self.assertEqual(code, 0, error)
        return run, draft

    def revision(self, folder, run, draft):
        plan, output = folder / "plan", folder / "revision"
        code, _, error = self.call("revision-plan", "--run", run, "--draft", draft,
            "--decisions", ROOT / "examples/revision/decisions.json", "--facts", ROOT / "examples/revision/facts.json",
            "--answers", ROOT / "examples/revision/answers.json", "--out", plan)
        self.assertEqual(code, 0, error)
        code, _, error = self.call("revise", "--run", run, "--draft", draft, "--plan", plan,
                                  "--fixtures", ROOT / "examples/revision/fixtures", "--out", output)
        self.assertEqual(code, 0, error)
        return output

    def test_full_cli_fixture_flow_preserves_source_and_writes_distinct_acceptance_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            run, draft = self.source(folder)
            before = draft.read_bytes()
            revision = self.revision(folder, run, draft)
            output = folder / "accepted.txt"
            code, _, error = self.call("finalize-revision", "--run", run, "--draft", draft,
                "--revision", revision, "--candidate", revision / "candidate.txt", "--out", output)
            self.assertEqual(code, 0, error)
            self.assertEqual(draft.read_bytes(), before)
            self.assertTrue(output.with_name(output.name + ".revision-acceptance.json").exists())
            self.assertFalse(output.with_name(output.name + ".approval.json").exists())
            self.assertNotEqual(output.read_bytes(), before)

    def test_finalize_rechecks_changed_draft_and_candidate_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            run, draft = self.source(folder)
            revision = self.revision(folder, run, draft)
            before = draft.read_bytes()
            draft.write_bytes(before + b"\nchanged")
            code, _, _ = self.call("finalize-revision", "--run", run, "--draft", draft,
                "--revision", revision, "--candidate", revision / "candidate.txt", "--out", folder / "no.txt")
            self.assertEqual(code, 1)
            self.assertFalse((folder / "no.txt").exists())
            draft.write_bytes(before)
            candidate = revision / "candidate.txt"
            candidate.write_bytes(candidate.read_bytes() + b" changed")
            code, _, _ = self.call("finalize-revision", "--run", run, "--draft", draft,
                "--revision", revision, "--candidate", candidate, "--out", folder / "no.txt")
            self.assertEqual(code, 1)
            self.assertFalse((folder / "no.txt").exists())

    def test_plan_template_and_blocked_candidate_have_no_final_essay(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            run, draft = self.source(folder)
            plan = folder / "plan"
            code, _, error = self.call("revision-plan", "--run", run, "--draft", draft, "--out", plan)
            self.assertEqual(code, 0, error)
            decisions = json.loads((plan / "decisions.json").read_text())["decisions"]
            self.assertTrue(all(d["decision"] == "defer" for d in decisions))
            output = folder / "candidate"
            code, _, error = self.call("revise", "--run", run, "--draft", draft, "--plan", plan,
                                      "--fixtures", ROOT / "examples/revision/fixtures", "--out", output)
            self.assertEqual(code, 2, error)
            self.assertTrue((output / "candidate.txt").exists())
            self.assertFalse((output / "final-essay.txt").exists())


if __name__ == "__main__":
    unittest.main()
