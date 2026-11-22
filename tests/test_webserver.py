import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from admissions_editor.webserver import WebApp, WebServer, normalize_input, WebError

ROOT = Path(__file__).resolve().parents[1]


class WebServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = WebApp(Path(self.tmp.name), ROOT / "examples/offline.toml")
        self.server = WebServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request("POST" if body is not None else "GET", path,
                           body=json.dumps(body).encode() if body is not None else None, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, response_headers, content

    def completed_demo(self):
        _, _, content = self.request("/api/demo")
        sample = json.loads(content)
        status, _, content = self.request("/api/reviews", {"mode": "demo", "input": sample})
        self.assertEqual(status, 202)
        job_id = json.loads(content)["id"]
        for _ in range(100):
            status, _, content = self.request(f"/api/reviews/{job_id}")
            job = json.loads(content)
            if job["state"] in {"complete", "partial", "failed"}:
                break
            time.sleep(.01)
        self.assertEqual(job["state"], "complete", job)
        return sample, job

    def test_static_page_and_assets_are_served_without_exposing_keys(self):
        status, headers, content = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'data-target="draft"', content)
        self.assertIn(b'Attach', content)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"OPENAI_API_KEY=", content)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/documents.js")[0], 200)
        self.assertEqual(self.request("/.env")[0], 404)
        self.assertEqual(self.request("/vendor/%2e%2e/webserver.py")[0], 404)
        self.assertIn("worker-src 'self'", headers["Content-Security-Policy"])

    def test_demo_review_approval_and_download_reuse_existing_version_guards(self):
        sample, job = self.completed_demo()
        result = job["result"]
        self.assertEqual(len(result["changes"]), 1)
        self.assertTrue(all(r["status"] == "complete" for r in result["reviewers"].values()))
        body = {"run_sha256": result["run_sha256"], "draft_sha256": result["draft_sha256"],
                "change_ids": [result["changes"][0]["id"]]}
        status, _, content = self.request(f"/api/reviews/{job['id']}/approve", body)
        self.assertEqual(status, 201, content)
        downloads = json.loads(content)
        status, _, revision = self.request(downloads["revision"])
        self.assertEqual(status, 200)
        self.assertEqual(revision.decode(), sample["draft"].replace("the the replay tool", "the replay tool"))
        self.assertEqual((Path(self.tmp.name) / job["id"] / "draft.txt").read_text(), sample["draft"])
        receipt = json.loads(self.request(downloads["receipt"])[2])
        self.assertEqual(receipt["approved_change_ids"], body["change_ids"])

    def test_stale_or_unknown_approval_is_rejected_without_revision(self):
        _, job = self.completed_demo()
        result = job["result"]
        for changes, run_hash in ((["CHG-unknown"], result["run_sha256"]), ([result["changes"][0]["id"]], "stale")):
            status, _, _ = self.request(f"/api/reviews/{job['id']}/approve", {
                "run_sha256": run_hash, "draft_sha256": result["draft_sha256"], "change_ids": changes})
            self.assertEqual(status, 409)
        self.assertFalse((Path(self.tmp.name) / job["id"] / "outputs").exists())

    def test_remote_api_requires_password_and_uses_http_only_session_cookie(self):
        remote = {"Host": "editor.example"}
        self.assertEqual(self.request("/api/config", headers=remote)[0], 401)
        self.app.password = "synthetic-web-access-password"
        self.assertEqual(self.request("/api/config")[0], 401)
        self.assertEqual(self.request("/api/session", {"password": "wrong"})[0], 401)
        status, headers, _ = self.request("/api/session", {"password": self.app.password}, {**remote, "Origin": "https://editor.example", "X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/api/config", headers={**remote, "Cookie": cookie})[0], 200)

    def test_forwarded_localhost_does_not_bypass_the_remote_password_requirement(self):
        self.assertEqual(self.request("/api/config", headers={"Host": "localhost", "X-Forwarded-Proto": "https"})[0], 401)

    def test_cross_site_submission_and_artifact_traversal_are_rejected(self):
        self.assertEqual(self.request("/api/reviews", {}, {"Origin": "https://other.example"})[0], 403)
        _, job = self.completed_demo()
        self.assertEqual(self.request(f"/api/reviews/{job['id']}/files/review/%2e%2e/input.json")[0], 404)
        self.assertEqual(self.request("/api/reviews/not-a-job")[0], 404)

    def test_upload_preflight_rejects_unsupported_names_and_duplicate_evidence(self):
        base = {"draft": "Synthetic draft.", "requirements": "Describe a project."}
        for evidence in ([{"id": "../private.txt", "text": "x"}], [{"id": "file.exe", "text": "x"}],
                         [{"id": "same.txt", "text": "x"}, {"id": "same.txt", "text": "y"}]):
            with self.assertRaises(WebError):
                normalize_input({**base, "evidence": evidence})
        self.assertEqual(self.request("/api/reviews", {"input": {**base, "draft": "x" * (128 * 1024 + 1)}})[0], 400)

    def test_extracted_document_evidence_preserves_original_filename_ids(self):
        result = normalize_input({"draft": "Synthetic draft.", "requirements": "Describe a project.",
                                  "evidence": [{"id": "team.pdf", "text": "Extracted team record."},
                                               {"id": "notes.docx", "text": "作者参与了 2 个实验。"}]})
        self.assertEqual(set(result["evidence"]), {"team.pdf", "notes.docx"})
        self.assertIn("2", result["evidence"]["notes.docx"])


if __name__ == "__main__":
    unittest.main()
