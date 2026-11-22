"""Default knowledge configuration and CLI hosts, using synthetic repositories."""

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from admissions_editor.cli import main
from admissions_editor.config import load_config
from admissions_editor.knowledge_sources import (assert_knowledge_current, essay_claims,
                                                prepare_knowledge, settings, unavailable_knowledge_context)
from admissions_editor.review import fingerprint, validate_knowledge_context


class KnowledgeDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "pyproject.toml").write_text('[project]\nname="admissions-editor-agent"\n')
        self.repo = self.root / "private/knowledge/repos/nearbite"
        self.repo.mkdir(parents=True)
        self.file = self.repo / "retrieval.py"
        self.file.write_text("# NearBite retrieves candidates with FAISS embeddings.\ndef retrieve_candidates(query):\n    return faiss_index.search(query)\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "Synthetic source")
        self.before = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.before)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT)

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = main(list(map(str, args)))
        return result, output.getvalue(), errors.getvalue()

    def context(self):
        return prepare_knowledge("For NearBite, I used FAISS embeddings for retrieval.", root=self.root)

    def test_defaults_load_without_repo_flags_and_missing_source_is_reported(self):
        options = settings(root=self.root)
        self.assertEqual(Path(options["index"]), self.root / "private/knowledge/index.sqlite3")
        context = self.context()
        self.assertEqual(context["status"], "partial")
        self.assertTrue(context["passages"])
        self.assertTrue(any("aitherpath" in message for message in context["diagnostics"]))
        self.assertTrue(validate_knowledge_context(context))
        assert_knowledge_current(context, root=self.root)

    def test_config_paths_and_explicit_overrides_replace_defaults(self):
        path = self.root / "custom.toml"
        path.write_text('[knowledge]\nlimit_per_claim=2\n[[knowledge.sources]]\nname="only"\npath="private/knowledge/repos/nearbite"\n')
        options = settings(knowledge_config=path, root=self.root)
        self.assertEqual([source["name"] for source in options["sources"]], ["only"])
        self.assertEqual(options["limit_per_claim"], 2)
        config = load_config(path)
        self.assertEqual(Path(config["knowledge"]["sources"][0]["path"]), self.repo)
        selected = settings(repos=[f"override={self.repo}"], root=self.root)
        self.assertEqual([source["name"] for source in selected["sources"]], ["override"])

    def test_index_and_private_root_cannot_escape_or_enter_sources(self):
        for index in ("tracked.sqlite3", "private/knowledge/repos/nearbite/index.sqlite3"):
            with self.assertRaises(ValueError):
                settings(config={"knowledge": {"index": index}}, root=self.root)
        with self.assertRaises(ValueError):
            settings(config={"knowledge": {"api_key": "synthetic"}}, root=self.root)
        with self.assertRaises(ValueError):
            settings(config={"knowledge": {"auto_refresh": "false"}}, root=self.root)

    def test_refresh_and_read_only_mode_never_serve_stale_passages(self):
        old = self.context()
        self.file.write_text("# NearBite uses BM25 ranking instead of FAISS retrieval.\n")
        stale = prepare_knowledge("NearBite FAISS retrieval", config={"knowledge": {"auto_refresh": False}}, root=self.root)
        self.assertFalse(stale["passages"])
        self.assertTrue(any(source["status"] == "stale" for source in stale["sources"]))
        with self.assertRaises(ValueError):
            assert_knowledge_current(old, root=self.root)
        refreshed = self.context()
        self.assertNotEqual(old["sources"][0]["snapshot_hash"], refreshed["sources"][0]["snapshot_hash"])

    def test_forged_passages_cannot_reuse_current_source_versions(self):
        original = self.context()
        for field, value in (("excerpt", "The applicant trained BERT."), ("line_start", 999), ("file_path", "forged.py")):
            changed = copy.deepcopy(original)
            changed["passages"][0][field] = value
            changed["snapshot_sha256"] = fingerprint({key: value for key, value in changed.items() if key != "snapshot_sha256"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                assert_knowledge_current(changed, root=self.root)

    def test_synthetic_disable_and_cloud_unavailability_need_no_index(self):
        disabled = prepare_knowledge("NearBite FAISS", disabled=True, root=self.root)
        self.assertEqual(disabled["status"], "disabled")
        self.assertFalse(disabled["passages"])
        self.assertFalse((self.root / "private/knowledge/index.sqlite3").exists())
        unavailable = unavailable_knowledge_context("Local sources are unavailable in this cloud runtime.")
        self.assertEqual(unavailable["status"], "missing")
        self.assertTrue(validate_knowledge_context(unavailable))

    def test_paragraph_project_scope_preserves_the_verbatim_claim(self):
        text = "NearBite uses restaurant data. I implemented retrieval with FAISS embeddings."
        claims = essay_claims(text)
        claim = next(claim for claim in claims if claim["text"].startswith("I implemented"))
        self.assertEqual(claim["text"], "I implemented retrieval with FAISS embeddings.")
        self.assertIn("nearbite", claim["query"])

    def test_review_and_plan_cli_load_defaults_without_repo_arguments(self):
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        for role in ("structure", "admissions", "skeptical", "language"):
            (fixtures / f"{role}.json").write_text(json.dumps({"role": role, "findings": []}))
        draft, requirements = self.root / "draft.txt", self.root / "requirements.txt"
        draft.write_text("For NearBite, I used FAISS embeddings for retrieval.")
        requirements.write_text("Describe your contribution. Maximum 500 words.")
        code, _, _ = self.cli("review", "--draft", draft, "--requirements", requirements, "--fixtures", fixtures, "--out", "review")
        self.assertEqual(code, 0)
        run = json.loads((self.root / "review/run.json").read_text())
        self.assertTrue(run["snapshot"]["knowledge_context"]["passages"])
        report = (self.root / "review/report.md").read_text()
        self.assertIn("retrieval.py", report)
        self.assertIn("snapshot", report.lower())
        code, _, _ = self.cli("revision-plan", "--run", "review", "--draft", draft, "--out", "plan")
        self.assertEqual(code, 0)
        plan = json.loads((self.root / "plan/plan.json").read_text())
        self.assertTrue(plan["knowledge_context"]["passages"])
        self.file.write_text("# Updated NearBite retrieval implementation.\n")
        code, _, errors = self.cli("revise", "--run", "review", "--draft", draft, "--plan", "plan", "--fixtures", "not-called", "--out", "stale")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "stale").exists())
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
