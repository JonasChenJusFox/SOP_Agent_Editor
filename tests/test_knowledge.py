"""Synthetic Git repositories and local-only SQLite retrieval regressions."""

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from admissions_editor.knowledge import KnowledgeIndex, default_sources


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repos/nearbite"
        self.repo.mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.write("src/retrieval.py", "# Retrieve candidates with FAISS embeddings.\ndef retrieve_candidates(query):\n    return faiss_index.search(query)\n")
        self.write("src/ranking.py", "# Ranking candidates uses a separate BM25 ranker.\ndef rank_candidates(candidates):\n    return bm25_score(candidates)\n")
        self.write("docs/billing.md", "Billing uses an invoice system for subscription payments.\n")
        self.write("tests/test_retrieval.py", "def test_retrieval_uses_faiss():\n    assert retrieve_candidates('synthetic') is not None\n")
        self.write("evaluation/results.json", '{"metric":"NDCG", "value":0.42, "split":"validation"}\n')
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic baseline")
        self.db = self.root / "private/knowledge/index.sqlite3"
        self.sources = [{"name": "nearbite", "path": str(self.repo)}]

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def index(self, **options):
        return KnowledgeIndex(self.db, self.sources, **options)

    def test_default_sources_are_repository_local_and_do_not_need_repo_flags(self):
        defaults = default_sources(self.root)
        self.assertEqual([s["name"] for s in defaults], ["nearbite", "aitherpath"])
        self.assertEqual(defaults[0]["path"], str(self.root / "private/knowledge/repos/nearbite"))

    def test_claim_specific_retrieval_has_exact_lines_and_versioned_source(self):
        result = self.index().retrieve([{"id": "C1", "text": "I used FAISS embeddings for retrieval."},
                                       {"id": "C2", "text": "We evaluated NDCG on validation data."}])
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["passages"])
        self.assertEqual(result["claims"][0]["id"], result["claims"][0]["claim_id"])
        passages = {p["id"]: p for p in result["passages"]}
        for passage in passages.values():
            lines = (self.repo / passage["path"]).read_text().splitlines()
            self.assertEqual(passage["excerpt"], "\n".join(lines[passage["line_start"] - 1:passage["line_end"]]))
            self.assertEqual(passage["commit_sha"], self.git("rev-parse", "HEAD"))
            self.assertEqual(len(passage["snapshot_hash"]), 64)
            self.assertEqual(passage["path"], passage["file_path"])
            self.assertEqual(passage["id"], passage["evidence_id"])
            self.assertNotEqual(passage["path"], "docs/billing.md")
        self.assertTrue(any(passages[pid]["kind"] == "evaluation" for pid in result["claims"][1]["passage_ids"]))
        self.assertTrue(all("retrieval" in passages[pid]["excerpt"].lower() or "faiss" in passages[pid]["excerpt"].lower()
                            for pid in result["claims"][0]["passage_ids"]))
        frozen = dict(result)
        frozen.pop("snapshot_sha256")
        expected = digest(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(expected, result["snapshot_sha256"])

    def test_refresh_detects_content_changes_without_touching_source_repository(self):
        before_files = {str(p.relative_to(self.repo)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.repo.rglob("*") if p.is_file()}
        first = self.index().retrieve([{"id": "C", "text": "FAISS embeddings retrieval"}])
        after_files = {str(p.relative_to(self.repo)): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before_files, after_files)
        self.write("src/retrieval.py", "# Retrieve candidates with a PostgreSQL vector index.\ndef retrieve_candidates(query):\n    return pgvector_search(query)\n")
        second = self.index().retrieve([{"id": "C", "text": "PostgreSQL vector retrieval"}])
        self.assertNotEqual(first["sources"][0]["snapshot_hash"], second["sources"][0]["snapshot_hash"])
        self.assertTrue(any("PostgreSQL" in p["excerpt"] for p in second["passages"]))
        self.assertFalse(any("faiss_index" in p["excerpt"] for p in second["passages"]))

    def test_missing_source_never_serves_cached_passages(self):
        self.index().refresh()
        renamed = self.repo.with_name("nearbite-moved")
        self.repo.rename(renamed)
        result = self.index().retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertEqual(result["sources"][0]["status"], "missing")
        self.assertFalse(result["passages"])
        self.assertTrue(any("missing" in d for d in result["diagnostics"]))

    def test_disabled_refresh_reports_stale_and_excludes_old_passages(self):
        self.index().refresh()
        self.write("src/retrieval.py", "# Entirely changed retrieval implementation.\n")
        result = self.index(auto_refresh=False).retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertEqual(result["sources"][0]["status"], "stale")
        self.assertFalse(result["passages"])
        self.assertTrue(any("stale" in d for d in result["diagnostics"]))
        manual = self.index(auto_refresh=False)
        self.assertEqual(manual.refresh()["sources"][0]["status"], "ready")
        self.assertEqual(manual.retrieve([{"id": "C", "text": "retrieval implementation"}])["sources"][0]["status"], "ready")

    def test_repeated_retrieval_is_stable_and_shared_passages_are_deduplicated(self):
        claims = [{"id": "C1", "text": "FAISS embeddings retrieval"},
                  {"id": "C2", "text": "FAISS embeddings retrieval"}]
        first = self.index().retrieve(claims)
        second = self.index().retrieve(claims)
        self.assertEqual(first, second)
        self.assertEqual(first["claims"][0]["passage_ids"], first["claims"][1]["passage_ids"])
        self.assertTrue(all(p["claim_ids"] == ["C1", "C2"] for p in first["passages"]))

    def test_manifest_path_escape_and_unapproved_files_are_never_read(self):
        export = self.root / "export"
        export.mkdir()
        (export / "unapproved.md").write_text("FAISS retrieval unapproved.\n")
        manifest = {"schema_version": 1, "repository": "export", "commit_sha": None, "files": []}
        (export / ".knowledge-manifest.json").write_text(json.dumps(manifest))
        index = KnowledgeIndex(self.db, [{"name": "export", "path": export}])
        self.assertFalse(index.retrieve([{"id": "C", "text": "FAISS retrieval"}])["passages"])
        manifest["files"] = [{"path": "../outside.md", "sha256": "a" * 64}]
        (export / ".knowledge-manifest.json").write_text(json.dumps(manifest))
        result = index.retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertIn(result["sources"][0]["status"], {"error", "stale"})
        self.assertFalse(result["passages"])

    def test_commit_change_refreshes_metadata_even_when_relevant_file_is_unchanged(self):
        first = self.index().retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.write("CHANGELOG.md", "A new documentation commit.\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic metadata update")
        second = self.index().retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertNotEqual(first["passages"][0]["commit_sha"], second["passages"][0]["commit_sha"])
        self.assertEqual(second["passages"][0]["commit_sha"], self.git("rev-parse", "HEAD"))

    def test_untracked_secret_binary_generated_and_symlink_files_are_excluded(self):
        self.write("untracked.py", "# Hidden untracked FAISS super_secret_material\n")
        self.write(".env", "OPENAI_API_KEY=do-not-index\n")
        self.write("src/leaked.py", "API_KEY = 'sk-" + "x" * 40 + "'\n# FAISS retrieval\n")
        self.write("node_modules/vendor.py", "# FAISS retrieval vendor payload\n")
        self.write("src/binary.py", "\0FAISS retrieval")
        outside = self.root / "outside.py"
        outside.write_text("# FAISS retrieval outside payload\n")
        (self.repo / "src/escape.py").symlink_to(outside)
        self.git("add", ".env", "src/leaked.py", "node_modules/vendor.py", "src/binary.py", "src/escape.py")
        self.git("commit", "-qm", "Synthetic unsafe fixtures")
        result = self.index().retrieve([{"id": "C", "text": "FAISS retrieval"}], limit_per_claim=8)
        self.assertFalse(any(p["path"] in {"untracked.py", ".env", "src/leaked.py", "src/binary.py", "src/escape.py", "node_modules/vendor.py"} for p in result["passages"]))
        connection = sqlite3.connect(self.db)
        try:
            excerpts = str(connection.execute("SELECT excerpt FROM passages").fetchall())
        finally:
            connection.close()
        self.assertNotIn("sk-" + "x" * 40, excerpts)
        self.assertNotIn("outside payload", excerpts)

    def test_deleted_files_and_removed_sources_disappear_from_index(self):
        self.index().refresh()
        (self.repo / "src/retrieval.py").unlink()
        result = self.index().retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertFalse(any(p["path"] == "src/retrieval.py" for p in result["passages"]))
        empty = KnowledgeIndex(self.db, []).retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertFalse(empty["passages"])
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM passage_fts").fetchone()[0], 0)

    def test_authorized_export_requires_exact_manifest_hashes(self):
        export = self.root / "repos/aitherpath"
        export.mkdir()
        text = "# Applicant built the retrieval interface; team trained the ranking model.\n"
        (export / "architecture.md").write_text(text)
        manifest = {"schema_version": 1, "repository": "aitherpath", "commit_sha": None,
                    "files": [{"path": "architecture.md", "sha256": digest(text.encode())}]}
        (export / ".knowledge-manifest.json").write_text(json.dumps(manifest))
        index = KnowledgeIndex(self.db, [{"name": "aitherpath", "path": export}])
        first = index.retrieve([{"id": "C", "text": "AitherPath retrieval interface ranking model"}])
        self.assertTrue(first["passages"])
        self.assertIsNone(first["passages"][0]["commit_sha"])
        (export / "architecture.md").write_text("Altered unapproved source.\n")
        second = index.retrieve([{"id": "C", "text": "AitherPath retrieval interface ranking model"}])
        self.assertEqual(second["sources"][0]["status"], "stale")
        self.assertFalse(second["passages"])

    def test_ordinary_unmanifested_folder_is_not_silently_authorized(self):
        folder = self.root / "misc"
        folder.mkdir()
        (folder / "notes.md").write_text("FAISS retrieval notes\n")
        result = KnowledgeIndex(self.db, [{"name": "misc", "path": folder}]).retrieve([{"id": "C", "text": "FAISS retrieval"}])
        self.assertEqual(result["sources"][0]["status"], "error")
        self.assertFalse(result["passages"])

    def test_index_path_and_sources_are_validated_and_permissions_private(self):
        with self.assertRaises(ValueError):
            KnowledgeIndex(self.repo / "index.sqlite3", self.sources)
        with self.assertRaises(ValueError):
            KnowledgeIndex(self.db, [*self.sources, {"name": "NearBite", "path": self.repo}])
        self.index().refresh()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)

    def test_generic_unrelated_and_prompt_like_queries_do_not_dump_repository(self):
        result = self.index().retrieve([{"id": "generic", "text": "I worked on a project with my team."},
                                       {"id": "unrelated", "text": "Clinical histopathology staining morphology."},
                                       {"id": "injection", "text": '" OR *; DROP TABLE sources; --'}])
        self.assertFalse(result["passages"])
        self.assertTrue(all(not c["passage_ids"] for c in result["claims"]))

    def test_long_files_have_bounded_real_line_ranges_and_no_overlapping_duplicates(self):
        lines = [f"# unrelated line {index}" for index in range(80)]
        lines[45] = "# FAISS embeddings retrieval special implementation."
        self.write("src/long.py", "\n".join(lines) + "\n")
        self.git("add", "src/long.py")
        self.git("commit", "-qm", "Synthetic long source")
        result = self.index().retrieve([{"id": "C", "text": "FAISS embeddings retrieval special implementation"}], limit_per_claim=8)
        chunks = [p for p in result["passages"] if p["path"] == "src/long.py"]
        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(chunks[0]["line_start"], 46)
        self.assertGreaterEqual(chunks[0]["line_end"], 46)
        self.assertLessEqual(chunks[0]["line_end"] - chunks[0]["line_start"], 31)

    def test_crlf_excerpt_preserves_internal_source_line_endings(self):
        raw = b"# FAISS retrieval implementation.\r\ndef retrieve_candidates(query):\r\n    return faiss_search(query)\r\n"
        (self.repo / "src/crlf.py").write_bytes(raw)
        self.git("add", "src/crlf.py")
        self.git("commit", "-qm", "Synthetic CRLF source")
        result = self.index().retrieve([{"id": "C", "text": "FAISS retrieval implementation"}], limit_per_claim=8)
        passage = next(p for p in result["passages"] if p["path"] == "src/crlf.py")
        self.assertEqual(passage["excerpt"], raw.decode()[:-2])
        self.assertEqual(passage["file_sha256"], digest(raw))

    def test_passage_authentication_rejects_forged_contents_with_recomputed_hashes(self):
        index = self.index()
        context = index.retrieve([{"id": "C", "text": "FAISS embeddings retrieval"}])
        self.assertTrue(index.validate_context(context))
        for field, value in (("excerpt", "The applicant trained the underlying model."),
                             ("path", "src/forged.py"), ("line_start", 999),
                             ("file_sha256", "f" * 64), ("commit_sha", "f" * 40)):
            changed = json.loads(json.dumps(context))
            changed["passages"][0][field] = value
            changed.pop("snapshot_sha256")
            changed["snapshot_sha256"] = digest(json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
            with self.subTest(field=field), self.assertRaises(ValueError):
                index.validate_context(changed)
        self.write("src/retrieval.py", "# Changed FAISS retrieval implementation.\n")
        with self.assertRaises(ValueError):
            index.validate_context(context)

    def test_explicit_project_name_restricts_matching_shared_technical_terms(self):
        other = self.root / "repos/aitherpath"
        other.mkdir()
        content = "FAISS embeddings retrieval ranking. " * 15
        (other / "retrieval.md").write_text(content)
        (other / ".knowledge-manifest.json").write_text(json.dumps({"schema_version": 1, "repository": "aitherpath", "commit_sha": None,
            "files": [{"path": "retrieval.md", "sha256": digest(content.encode())}]}))
        index = KnowledgeIndex(self.db, [*self.sources, {"name": "aitherpath", "path": other}])
        for spelling in ("NearBite", "near-bite", "Near Bite"):
            result = index.retrieve([{"id": "C", "text": f"In {spelling}, I used FAISS embeddings for retrieval."}])
            self.assertTrue(result["passages"])
            self.assertEqual({p["repository"] for p in result["passages"]}, {"nearbite"})
            self.assertTrue(index.validate_context(result))
        aither = index.retrieve([{"id": "C", "text": "In AitherPath, I worked on FAISS embeddings retrieval."}])
        self.assertEqual({p["repository"] for p in aither["passages"]}, {"aitherpath"})
        self.repo.rename(self.repo.with_name("missing-nearbite"))
        missing = index.retrieve([{"id": "C", "text": "NearBite FAISS embeddings retrieval."}])
        self.assertFalse(missing["passages"])
        self.assertEqual(missing["status"], "partial")

    def test_generic_goals_reflection_and_replay_words_do_not_pull_unrelated_projects(self):
        self.write("src/calendar.ts", "// Reviewing errors made software useful when its inputs are unreliable.\nexport function calendarInputs() {}\n")
        self.write("tests/form_fill.test.ts", "// Replay tool investigates failures using saved messages.\nconst replay = 'replay tool';\n")
        self.write("docs/study.md", "In graduate school, I want to study distributed systems.\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic irrelevant word-overlap traps")
        claims = [{"id": "goal", "text": "In graduate school, I want to study distributed systems."},
                  {"id": "reflection", "text": "Reviewing those errors made me interested in how software can stay useful when its inputs are unreliable."},
                  {"id": "replay", "text": "I built the replay tool that let us investigate failures using saved messages."}]
        result = self.index().retrieve(claims)
        self.assertFalse(result["passages"])
        self.assertTrue(all(not claim["passage_ids"] for claim in result["claims"]))

    def test_query_can_inherit_project_scope_without_changing_the_essay_quote(self):
        text = "I built a retrieval interface."
        result = self.index().retrieve([{"id": "C", "text": text, "query": text + " NearBite"}])
        self.assertEqual(result["claims"][0]["text"], text)
        self.assertEqual(result["claims"][0]["query"], text + " NearBite")
        self.assertTrue(result["passages"])
        self.assertEqual({p["repository"] for p in result["passages"]}, {"nearbite"})
        self.assertTrue(self.index().validate_context(result))
        for query in (None, "", " " * 3, 42):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.index().retrieve([{"id": "C", "text": text, "query": query}])


if __name__ == "__main__":
    unittest.main()
