"""Persisted cloud session expiry/re-login without resubmitting editorial work."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_cloud_app import CloudApp, Database, Request, ROOT, Workflow
from admissions_editor.cloud_runtime import next_action, revision_demo_payload


class CountingWorkflow(Workflow):
    def __init__(self):
        self.created = []

    async def create(self, value):
        self.created.append(value["id"])
        return await super().create(value)


class UnreadBodyRequest(Request):
    async def bytes(self):
        raise AssertionError("An unauthorized protected request must not parse its body.")


class CloudSessionExpiryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1_900_000_000
        timer = patch("cloud_app.time.time", side_effect=lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)
        no_model = patch("admissions_editor.providers.OpenAIProvider._request",
                         side_effect=AssertionError("Session recovery must not call a model."))
        self.model_request = no_model.start()
        self.addCleanup(no_model.stop)
        self.db = Database()
        self.workflow = CountingWorkflow()
        self.password = "synthetic-session-test-password"
        self.env = SimpleNamespace(DB=self.db, REVIEWS=self.workflow,
            ADMISSIONS_WEB_PASSWORD=self.password,
            OPENAI_API_KEY="synthetic-not-a-key", OPENAI_MODEL="synthetic-model")
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        await self.login()

    async def asyncTearDown(self):
        self.model_request.assert_not_called()
        self.db.connection.close()

    async def call(self, path, method="GET", body=None, *, cookie=True, headers=None):
        values = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            values["Cookie"] = self.cookie
        return await self.app.handle(Request(path, method, body, values))

    async def login(self, password=None):
        response = await self.app.handle(Request("/api/session", "POST",
            {"password": password or self.password}, {"Content-Type": "application/json"}))
        self.assertEqual(response.status, 200, response.body)
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        return response

    async def completed_review(self):
        demo = revision_demo_payload(ROOT / "examples")
        submitted = await self.call("/api/reviews", "POST", {"mode": "demo", "input": demo["input"]})
        self.assertEqual(submitted.status, 202, submitted.body)
        job_id = json.loads(submitted.body)["id"]
        job = await self.app.store.job(job_id)
        output = next_action(json.loads(job["input_json"]), "demo", json.loads(job["config_json"]), {}, ROOT / "examples")
        self.assertEqual(output["run"]["status"], "complete")
        await self.app.store.write(job_id, {"review/" + name: text for name, text in output["artifacts"].items()},
                                   ("complete", "Synthetic completed review"))
        response = await self.call("/api/reviews/" + job_id)
        self.assertEqual(response.status, 200)
        review = json.loads(response.body)
        body = {"run_sha256": review["result"]["run_sha256"], **demo["plan_input"]}
        return job_id, body

    async def test_expiry_rejects_plan_before_body_then_login_recovers_same_review(self):
        job_id, body = await self.completed_review()
        route = f"/api/reviews/{job_id}/revision-plan"
        before = await self.call(route, "POST", body)
        self.assertEqual(before.status, 200, before.body)
        frozen = await self.app.store.read(job_id, "review/run.json")
        files = self.db.connection.execute("SELECT count(*) FROM files").fetchone()[0]
        old_cookie = self.cookie
        self.now += 43200

        session = json.loads((await self.call("/api/session")).body)
        self.assertFalse(session["authenticated"])
        response = await self.app.handle(UnreadBodyRequest(route, "POST", body,
            {"Cookie": old_cookie, "Content-Type": "application/json"}))
        self.assertEqual(response.status, 401)
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM files").fetchone()[0], files)
        self.assertEqual(self.workflow.created, [job_id])

        await self.login()
        self.assertNotEqual(self.cookie, old_cookie)
        self.assertEqual((await self.call("/api/reviews/" + job_id)).status, 200)
        after = await self.call(route, "POST", body)
        self.assertEqual(after.status, 200, after.body)
        self.assertEqual(json.loads(after.body)["plan"]["plan_sha256"], json.loads(before.body)["plan"]["plan_sha256"])
        self.assertEqual(await self.app.store.read(job_id, "review/run.json"), frozen)
        self.assertEqual(self.workflow.created, [job_id])

    async def test_cookie_lifetime_matches_server_expiry_and_new_instance_preserves_it(self):
        response = await self.login()
        cookie = response.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=43200"):
            self.assertIn(attribute, cookie)
        token = self.cookie.split("=", 1)[1]
        row = self.db.connection.execute("SELECT expires_at FROM sessions WHERE token_hash=?",
                                         (self.app.session_hash(token),)).fetchone()
        self.assertEqual(row["expires_at"], self.now + 43200)
        self.now += 43199
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        self.assertEqual((await self.call("/api/config")).status, 200)
        self.now += 1
        self.assertEqual((await self.call("/api/config")).status, 401)

    async def test_password_rotation_invalidates_cookie_without_losing_review(self):
        job_id, body = await self.completed_review()
        self.password = "synthetic-rotated-site-password"
        self.env.ADMISSIONS_WEB_PASSWORD = self.password
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        route = f"/api/reviews/{job_id}/revision-plan"
        self.assertEqual((await self.call(route, "POST", body)).status, 401)
        await self.login()
        self.assertEqual((await self.call(route, "POST", body)).status, 200)
        self.assertEqual(self.workflow.created, [job_id])

    async def test_missing_or_expired_cookie_never_bypasses_csrf_or_reads_plan_body(self):
        job_id, body = await self.completed_review()
        route = f"/api/reviews/{job_id}/revision-plan"
        request = UnreadBodyRequest(route, "POST", body, {"Content-Type": "application/json"})
        self.assertEqual((await self.app.handle(request)).status, 401)
        self.now += 43200
        self.assertEqual((await self.call(route, "POST", body,
            headers={"Origin": "https://different.example"})).status, 403)
        self.assertEqual((await self.call(route, "POST", body,
            headers={"Sec-Fetch-Site": "cross-site"})).status, 403)
        wrong = await self.call("/api/session", "POST", {"password": "synthetic-wrong-password"}, cookie=False)
        self.assertEqual(wrong.status, 401)
        self.assertFalse(json.loads((await self.call("/api/session")).body)["authenticated"])
        self.assertEqual(self.workflow.created, [job_id])


if __name__ == "__main__":
    unittest.main()
