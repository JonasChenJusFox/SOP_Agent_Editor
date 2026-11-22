"""Repository-default knowledge in local HTTP flows; synthetic repositories only."""

import copy
import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from admissions_editor.cloud_runtime import demo_input, next_action, prepare_input
from admissions_editor.providers import ROLE_NAMES, RoutedProvider
from admissions_editor.webserver import WebApp, WebServer
from admissions_editor.workflow import RuleWorkflowProvider
from test_technical_integration import TechnicalEditor

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
DRAFT = "In NearBite, I implemented candidate retrieval with FAISS embeddings."


class ClearReviewer:
    label = "fixture:repository-clear-review"

    def review(self, role, payload):
        return {"role": role, "findings": []}


class KnowledgeWebTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        (self.base / "pyproject.toml").write_text('[project]\nname="admissions-editor-agent"\n')
        self.examples = self.base / "examples"
        self.examples.mkdir()
        for name in ("draft.txt", "requirements.txt", "preferences.txt", "claims.json", "sources.json"):
            shutil.copyfile(EXAMPLES / name, self.examples / name)
        for name in ("fixtures", "evidence", "revision"):
            shutil.copytree(EXAMPLES / name, self.examples / name)
        self.repo = self.base / "private/knowledge/repos/nearbite"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        (self.repo / "src").mkdir()
        self.code = self.repo / "src/retrieval.py"
        self.code.write_text("# NearBite candidate retrieval with FAISS embeddings\ndef retrieve_candidates(query, index):\n    return index.search(query)\n")
        (self.repo / "README.md").write_text("# NearBite\nCandidate retrieval uses FAISS embeddings.\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "Synthetic knowledge fixture"], check=True, capture_output=True)
        self.app = WebApp(self.base / "runs", examples_dir=self.examples)
        self.app.configured_available = True
        self.app.configured_revision_available = True
        self.app.providers = lambda: (RoutedProvider({role: ClearReviewer() for role in ROLE_NAMES}), RuleWorkflowProvider())
        self.server = WebServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()
        self.temporary.cleanup()

    def request(self, path, body=None, expected=200):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request("POST" if body is not None else "GET", path,
                     body=json.dumps(body).encode() if body is not None else None,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        raw, status = response.read(), response.status
        conn.close()
        self.assertEqual(status, expected, raw)
        return json.loads(raw) if raw.startswith((b"{", b"[")) else raw

    def wait(self, job_id, kind="reviews"):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.request(f"/api/{kind}/{job_id}")
            if job["state"] in {"complete", "partial", "failed"}:
                self.assertNotEqual(job["state"], "failed", job)
                return job
            time.sleep(.01)
        self.fail("Synthetic local job did not finish")

    def review(self):
        body = {"mode": "configured", "input": {"draft": DRAFT,
            "requirements": "Describe your actual technical contribution. Maximum 500 English words.",
            "preferences": "Keep technical meaning.", "evidence": []}}
        submitted = self.request("/api/reviews", body, 202)
        return self.wait(submitted["id"])

    def test_default_sources_retrieve_claim_passages_without_repository_arguments(self):
        before = self.code.read_bytes()
        job = self.review()
        context = job["result"]["knowledge_context"]
        self.assertTrue(context["passages"])
        self.assertTrue(any(source["name"] == "aitherpath" and source["status"] == "missing" for source in context["sources"]))
        self.assertTrue(all(passage["repository"] == "nearbite" for passage in context["passages"]))
        passage = context["passages"][0]
        self.assertTrue(passage["commit_sha"])
        self.assertTrue(passage["snapshot_hash"])
        self.assertGreaterEqual(passage["line_start"], 1)
        self.assertGreaterEqual(passage["line_end"], passage["line_start"])
        self.assertIn("FAISS", passage["excerpt"])
        frozen = self.request(f"/api/reviews/{job['id']}/files/review/knowledge.json")
        self.assertEqual(frozen, context)
        report = self.request(f"/api/reviews/{job['id']}/files/review/report.md").decode()
        self.assertIn(passage["file_path"], report)
        self.assertIn(passage["snapshot_hash"][:12], report)
        self.assertEqual(before, self.code.read_bytes())
        self.assertTrue((self.base / "private/knowledge/index.sqlite3").is_file())

    def test_missing_default_sources_are_reported_before_any_review(self):
        status = self.request("/api/config")["knowledge_status"]
        self.assertTrue(any(source["name"] == "aitherpath" and source["status"] == "missing" for source in status["sources"]))
        self.assertTrue(status["diagnostics"])
        self.assertNotIn("passages", status)

    def test_changed_repository_blocks_stale_revision_submission_and_local_approval(self):
        job = self.review()
        body = {"run_sha256": job["result"]["run_sha256"], "decisions": [], "confirmed_facts": [], "answers": {}}
        plan = self.request(f"/api/reviews/{job['id']}/revision-plan", body)["plan"]
        self.assertTrue(plan["knowledge_context"]["passages"])
        self.code.write_text(self.code.read_text() + "# Candidate retrieval now applies a bounded search filter.\n")
        rejected = self.request(f"/api/reviews/{job['id']}/revisions", {"run_sha256": job["result"]["run_sha256"], "plan": plan}, 409)
        self.assertIn("来源", rejected["error"])
        self.request(f"/api/reviews/{job['id']}/approve", {"run_sha256": job["result"]["run_sha256"],
            "draft_sha256": job["result"]["draft_sha256"], "change_ids": ["CHG-not-real"]}, 409)
        self.assertEqual(len(self.app.jobs), 1)

    def test_finalization_checks_live_repository_again_after_generation(self):
        job = self.review()
        plan = self.request(f"/api/reviews/{job['id']}/revision-plan", {"run_sha256": job["result"]["run_sha256"], "decisions": []})["plan"]
        with patch("admissions_editor.webserver.revision_provider", return_value=TechnicalEditor(essay=DRAFT)):
            submitted = self.request(f"/api/reviews/{job['id']}/revisions", {"run_sha256": job["result"]["run_sha256"], "plan": plan}, 202)
            revision = self.wait(submitted["id"], "revisions")
        self.assertEqual(revision["result"]["status"], "ready", revision)
        result = revision["result"]
        self.assertTrue(result["knowledge_context"]["passages"])
        self.code.write_text(self.code.read_text() + "# Changed after the generated candidate.\n")
        self.request(f"/api/revisions/{revision['id']}/finalize", {"candidate_sha256": result["candidate_sha256"],
            "plan_sha256": result["plan_sha256"]}, 409)
        self.assertFalse((self.app.root / revision["id"] / "outputs").exists())

    def test_builtin_demo_explicitly_skips_private_sources(self):
        demo = self.request("/api/revision-demo")
        submitted = self.request("/api/reviews", {"mode": "demo", "input": demo["input"]}, 202)
        review = self.wait(submitted["id"])
        context = review["result"]["knowledge_context"]
        self.assertEqual(context["status"], "disabled")
        self.assertEqual(context["passages"], [])
        plan = self.request(f"/api/reviews/{review['id']}/revision-plan", {**demo["plan_input"], "run_sha256": review["result"]["run_sha256"]})["plan"]
        self.assertEqual(plan["knowledge_context"]["status"], "disabled")


class CloudKnowledgeIsolationTest(unittest.TestCase):
    def test_cloud_does_not_open_local_sources_or_index(self):
        inputs = prepare_input(demo_input(EXAMPLES), "demo", EXAMPLES)
        with patch("admissions_editor.knowledge_sources.prepare_knowledge", side_effect=AssertionError("No local source reads in cloud")):
            result = next_action(inputs, "demo", {}, {}, EXAMPLES)
        self.assertTrue(result["complete"])
        context = result["run"]["snapshot"]["knowledge_context"]
        self.assertEqual(context["status"], "disabled")
        self.assertEqual(context["passages"], [])
        self.assertTrue(context["diagnostics"])


if __name__ == "__main__":
    unittest.main()
