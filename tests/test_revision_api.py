"""Synthetic, offline checks for the local and D1 full-essay APIs."""

import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from admissions_editor.cloud_runtime import next_action, next_revision_action
from admissions_editor.review import sha256
from admissions_editor.webserver import WebApp, WebServer

# Reuse the existing SQLite-backed D1/Workflow request substitutes. No Cloudflare
# account, API key, network model call or applicant material is used by these tests.
from test_cloud_app import CloudApp, Database, Request, Workflow

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "examples/revision/expected-final.txt"


def accept_body(result):
    return {"candidate_sha256": result["candidate_sha256"], "plan_sha256": result["plan_sha256"]}


class LocalRevisionApiTest(unittest.TestCase):
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
        self.thread.join(timeout=2)
        self.app.close()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request("POST" if body is not None else "GET", path,
                           body=json.dumps(body).encode() if body is not None else None,
                           headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def json_request(self, path, body=None, expected=200):
        status, _, content = self.request(path, body)
        self.assertEqual(status, expected, content)
        return json.loads(content)

    def completed_job(self, job_id, kind="reviews"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.json_request(f"/api/{kind}/{job_id}")
            if job["state"] in {"complete", "partial", "failed"}:
                self.assertNotEqual(job["state"], "failed", job)
                return job
            time.sleep(.01)
        self.fail("Offline fixture job did not finish.")

    def completed_review(self):
        demo = self.json_request("/api/revision-demo")
        submitted = self.json_request("/api/reviews", {"mode": "demo", "input": demo["input"]}, 202)
        job = self.completed_job(submitted["id"])
        self.assertEqual(job["state"], "complete", job)
        return demo, job

    def plan(self, demo, review, overrides=None):
        data = {**copy.deepcopy(demo["plan_input"]), **(overrides or {}),
                "run_sha256": review["result"]["run_sha256"]}
        return self.json_request(f"/api/reviews/{review['id']}/revision-plan", data)["plan"]

    def completed_revision(self, review, plan):
        submitted = self.json_request(f"/api/reviews/{review['id']}/revisions",
            {"run_sha256": review["result"]["run_sha256"], "plan": plan}, 202)
        return self.completed_job(submitted["id"], "revisions")

    def test_plan_full_candidate_explicit_acceptance_and_download(self):
        demo, review = self.completed_review()
        default = self.json_request(f"/api/reviews/{review['id']}/revision-plan")["plan"]
        self.assertTrue(default["findings"])
        self.assertTrue(all(f["decision"] == "defer" for f in default["findings"]))
        plan = self.plan(demo, review)
        self.assertEqual(plan["review_sha256"], review["result"]["run_sha256"])
        revision = self.completed_revision(review, plan)
        result = revision["result"]
        self.assertEqual(result["status"], "ready", result.get("blockers"))
        self.assertEqual(result["candidate_text"], EXPECTED.read_text())
        self.assertTrue(result["validation"]["passed"])
        self.assertEqual(len(result["trace"]), len(plan["findings"]))
        self.assertEqual([s["stage"] for s in result["stages"]],
                         ["sop_revise", "polish", "humanize", "verify_revision"])
        self.assertIn("final-essay.txt", result["artifacts"])
        self.assertFalse((self.app.root / revision["id"] / "outputs").exists())
        finalized = self.json_request(f"/api/revisions/{revision['id']}/finalize", accept_body(result), 201)
        status, headers, text = self.request(finalized["revision"])
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(text.decode(), EXPECTED.read_text())
        receipt = self.json_request(finalized["receipt"])
        self.assertEqual(receipt["author_action"], "accept_full_revision")
        self.assertEqual(receipt["final_sha256"], sha256(text))
        self.assertEqual(receipt["plan_sha256"], plan["plan_sha256"])
        self.assertEqual((self.app.root / review["id"] / "draft.txt").read_text(), demo["input"]["draft"])

    def test_blocked_candidate_has_no_final_download_or_acceptance(self):
        demo, review = self.completed_review()
        plan = self.plan(demo, review, {"word_limit": 1})
        revision = self.completed_revision(review, plan)
        result = revision["result"]
        self.assertEqual(revision["state"], "partial")
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["candidate_text"])
        self.assertTrue(result["blockers"])
        self.assertNotIn("final_text", result)
        self.assertNotIn("final-essay.txt", result["artifacts"])
        self.assertEqual(self.request(f"/api/revisions/{revision['id']}/files/candidate.txt")[0], 200)
        self.assertEqual(self.request(f"/api/revisions/{revision['id']}/files/final-essay.txt")[0], 404)
        self.assertEqual(self.request(f"/api/revisions/{revision['id']}/finalize", accept_body(result))[0], 409)
        self.assertFalse((self.app.root / revision["id"] / "outputs").exists())

    def test_invalid_schema_stale_plan_and_exact_acceptance_hashes(self):
        demo, review = self.completed_review()
        route = f"/api/reviews/{review['id']}/revision-plan"
        body = {**demo["plan_input"], "run_sha256": review["result"]["run_sha256"]}
        self.assertEqual(self.request(route, {**body, "decisions": []})[0], 400)
        self.assertEqual(self.request(route, {**body, "run_sha256": "0" * 64})[0], 409)
        plan = self.plan(demo, review)
        changed = copy.deepcopy(plan)
        changed["confirmed_facts"][0]["text"] += " An unreviewed addition."
        before = len(self.app.jobs)
        self.assertEqual(self.request(f"/api/reviews/{review['id']}/revisions",
            {"run_sha256": body["run_sha256"], "plan": changed})[0], 409)
        self.assertEqual(len(self.app.jobs), before)
        revision = self.completed_revision(review, plan)
        self.assertEqual(revision["result"]["status"], "ready")
        good = accept_body(revision["result"])
        route = f"/api/revisions/{revision['id']}/finalize"
        self.assertEqual(self.request(route, {})[0], 400)
        for field in ("candidate_sha256", "plan_sha256"):
            with self.subTest(field=field):
                self.assertEqual(self.request(route, {**good, field: "0" * 64})[0], 409)
        self.assertFalse((self.app.root / revision["id"] / "outputs").exists())

    def test_changed_source_draft_or_review_blocks_acceptance(self):
        demo, review = self.completed_review()
        revision = self.completed_revision(review, self.plan(demo, review))
        self.assertEqual(revision["result"]["status"], "ready")
        body = accept_body(revision["result"])
        route = f"/api/revisions/{revision['id']}/finalize"
        draft_path = self.app.root / review["id"] / "draft.txt"
        draft_path.write_text(demo["input"]["draft"] + "\nChanged after review.\n")
        self.assertEqual(self.request(route, body)[0], 409)
        draft_path.write_text(demo["input"]["draft"])
        run_path = self.app.root / review["id"] / "review/run.json"
        run_path.write_bytes(run_path.read_bytes() + b"\n")
        self.assertEqual(self.request(route, body)[0], 409)
        self.assertFalse((self.app.root / revision["id"] / "outputs").exists())

    def test_new_routes_require_authentication_and_correct_job_kind(self):
        demo, review = self.completed_review()
        revision = self.completed_revision(review, self.plan(demo, review))
        self.assertEqual(self.request(f"/api/revisions/{review['id']}")[0], 404)
        self.assertEqual(self.request(f"/api/reviews/{revision['id']}/revision-plan")[0], 404)
        self.assertEqual(self.request(f"/api/revisions/{revision['id']}/files/../plan.json")[0], 404)
        self.app.password = "synthetic-revision-password"
        for path, body in (("/api/revision-demo", None), ("/api/skills", None),
                           (f"/api/reviews/{review['id']}/revision-plan", None),
                           (f"/api/reviews/{review['id']}/revisions", {}),
                           (f"/api/revisions/{revision['id']}", None),
                           (f"/api/revisions/{revision['id']}/finalize", accept_body(revision["result"]))):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, body)[0], 401)


class CloudRevisionApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database()
        self.env = SimpleNamespace(DB=self.db, REVIEWS=Workflow(),
            ADMISSIONS_WEB_PASSWORD="synthetic-revision-password",
            OPENAI_API_KEY="synthetic-not-a-real-key", OPENAI_MODEL="synthetic-model")
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        response = await self.call("/api/session", "POST", {"password": self.env.ADMISSIONS_WEB_PASSWORD})
        self.assertEqual(response.status, 200, response.body)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

    async def asyncTearDown(self):
        self.db.connection.close()

    async def call(self, path, method="GET", body=None, cookie=True):
        headers = {"Content-Type": "application/json"}
        if cookie and hasattr(self, "cookie"):
            headers["Cookie"] = self.cookie
        return await self.app.handle(Request(path, method, body, headers))

    async def json_call(self, path, method="GET", body=None, expected=200):
        response = await self.call(path, method, body)
        self.assertEqual(response.status, expected, response.body)
        return json.loads(response.body)

    async def completed_review(self):
        demo = await self.json_call("/api/revision-demo")
        submitted = await self.json_call("/api/reviews", "POST", {"mode": "demo", "input": demo["input"]}, 202)
        row = await self.app.store.job(submitted["id"])
        result = next_action(json.loads(row["input_json"]), "demo", json.loads(row["config_json"]), {}, ROOT / "examples")
        self.assertTrue(result["complete"])
        self.assertEqual(result["run"]["status"], "complete")
        await self.app.store.write(row["id"], {"review/" + k: v for k, v in result["artifacts"].items()}, ("complete", "审稿完成"))
        return demo, await self.json_call(f"/api/reviews/{row['id']}")

    async def plan(self, demo, review, overrides=None):
        data = {**copy.deepcopy(demo["plan_input"]), **(overrides or {}),
                "run_sha256": review["result"]["run_sha256"]}
        return (await self.json_call(f"/api/reviews/{review['id']}/revision-plan", "POST", data))["plan"]

    async def completed_revision(self, review, plan):
        submitted = await self.json_call(f"/api/reviews/{review['id']}/revisions", "POST",
            {"run_sha256": review["result"]["run_sha256"], "plan": plan}, 202)
        row = await self.app.store.job(submitted["id"])
        source = json.loads(await self.app.store.read(review["id"], "review/run.json"))
        result = next_revision_action(source, json.loads(row["input_json"])["plan"],
                                      json.loads(row["config_json"]), {}, ROOT / "examples", row["mode"])
        self.assertTrue(result["complete"])
        state = "complete" if result["record"]["status"] == "ready" else "partial"
        await self.app.store.write(row["id"], {"revision/" + k: v for k, v in result["artifacts"].items()}, (state, "全文候选已生成"))
        return await self.json_call(f"/api/revisions/{row['id']}")

    async def test_cloud_full_revision_persists_and_requires_exact_acceptance(self):
        demo, review = await self.completed_review()
        default = (await self.json_call(f"/api/reviews/{review['id']}/revision-plan"))["plan"]
        self.assertTrue(all(f["decision"] == "defer" for f in default["findings"]))
        plan = await self.plan(demo, review)
        revision = await self.completed_revision(review, plan)
        self.assertEqual(revision["state"], "complete", revision)
        self.assertEqual(revision["result"]["status"], "ready")
        self.assertEqual(revision["result"]["candidate_text"], EXPECTED.read_text())
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        again = await self.json_call(f"/api/revisions/{revision['id']}")
        self.assertEqual(again["result"]["candidate_sha256"], revision["result"]["candidate_sha256"])
        body = accept_body(again["result"])
        route = f"/api/revisions/{revision['id']}/finalize"
        for field in ("candidate_sha256", "plan_sha256"):
            with self.subTest(field=field):
                self.assertEqual((await self.call(route, "POST", {**body, field: "0" * 64})).status, 409)
        finalized = await self.json_call(route, "POST", body, 201)
        text = await self.call(finalized["revision"])
        receipt = await self.json_call(finalized["receipt"])
        self.assertEqual(text.status, 200)
        self.assertEqual(text.body, EXPECTED.read_text())
        self.assertEqual(receipt["final_sha256"], sha256(text.body.encode()))
        self.assertEqual(receipt["review_sha256"], review["result"]["run_sha256"])
        original = json.loads((await self.app.store.job(review["id"]))["input_json"])
        self.assertEqual(original["draft"], demo["input"]["draft"])

    async def test_cloud_blocked_candidate_cannot_publish_final(self):
        demo, review = await self.completed_review()
        revision = await self.completed_revision(review, await self.plan(demo, review, {"word_limit": 1}))
        result = revision["result"]
        self.assertEqual(revision["state"], "partial")
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["blockers"])
        self.assertNotIn("final_text", result)
        self.assertNotIn("final-essay.txt", result["artifacts"])
        self.assertEqual((await self.call(f"/api/revisions/{revision['id']}/files/candidate.txt")).status, 200)
        self.assertEqual((await self.call(f"/api/revisions/{revision['id']}/files/final-essay.txt")).status, 404)
        self.assertEqual((await self.call(f"/api/revisions/{revision['id']}/finalize", "POST", accept_body(result))).status, 409)
        count = await self.app.store.sql("SELECT count(*) AS n FROM files WHERE job_id=? AND path LIKE 'outputs/%'", revision["id"]).first()
        self.assertEqual(count["n"], 0)

    async def test_cloud_schema_and_stale_source_are_rejected_without_output(self):
        demo, review = await self.completed_review()
        route = f"/api/reviews/{review['id']}/revision-plan"
        body = {**demo["plan_input"], "run_sha256": review["result"]["run_sha256"]}
        self.assertEqual((await self.call(route, "POST", {**body, "decisions": []})).status, 400)
        self.assertEqual((await self.call(route, "POST", {**body, "run_sha256": "0" * 64})).status, 409)
        plan = await self.plan(demo, review)
        changed = copy.deepcopy(plan)
        changed["confirmed_facts"][0]["text"] += " Unreviewed source."
        self.assertEqual((await self.call(f"/api/reviews/{review['id']}/revisions", "POST",
            {"run_sha256": body["run_sha256"], "plan": changed})).status, 409)
        revision = await self.completed_revision(review, plan)
        self.assertEqual(revision["result"]["status"], "ready")
        await self.app.store.sql("UPDATE files SET content=content||? WHERE job_id=? AND path='review/run.json' AND chunk=0",
                                 "\n", review["id"]).run()
        self.assertEqual((await self.call(f"/api/revisions/{revision['id']}/finalize", "POST",
            accept_body(revision["result"]))).status, 409)
        count = await self.app.store.sql("SELECT count(*) AS n FROM files WHERE job_id=? AND path LIKE 'outputs/%'", revision["id"]).first()
        self.assertEqual(count["n"], 0)

    async def test_cloud_new_routes_enforce_auth_and_job_kind(self):
        demo, review = await self.completed_review()
        revision = await self.completed_revision(review, await self.plan(demo, review))
        self.assertEqual((await self.call(f"/api/revisions/{review['id']}")).status, 404)
        self.assertEqual((await self.call(f"/api/reviews/{revision['id']}/revision-plan")).status, 404)
        self.assertEqual((await self.call(f"/api/reviews/{revision['id']}")).status, 404)
        self.assertEqual((await self.call(f"/api/revisions/{revision['id']}/files/../plan.json")).status, 404)
        for path, method, body in (("/api/revision-demo", "GET", None), ("/api/skills", "GET", None),
                (f"/api/reviews/{review['id']}/revision-plan", "GET", None),
                (f"/api/reviews/{review['id']}/revisions", "POST", {}),
                (f"/api/revisions/{revision['id']}", "GET", None),
                (f"/api/revisions/{revision['id']}/finalize", "POST", accept_body(revision["result"]))):
            with self.subTest(path=path):
                self.assertEqual((await self.call(path, method, body, cookie=False)).status, 401)


if __name__ == "__main__":
    unittest.main()
