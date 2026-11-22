import asyncio
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from admissions_editor.cloud_runtime import next_action
from admissions_editor.review import sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloudflare/python"))
from cloud_app import CloudApp, Store


class Statement:
    def __init__(self, db, sql, values=()):
        self.db, self.sql, self.values = db, sql, values

    def bind(self, *values):
        return Statement(self.db, self.sql, values)

    def execute(self):
        return self.db.connection.execute(self.sql, self.values)

    async def first(self):
        row = self.execute().fetchone()
        return dict(row) if row else None

    async def all(self):
        return {"results": [dict(r) for r in self.execute().fetchall()]}

    async def run(self):
        cursor = self.execute()
        self.db.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        for migration in sorted((ROOT / "cloudflare/migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return Statement(self, sql)

    async def batch(self, statements):
        with self.connection:
            for statement in statements:
                statement.execute()


class Workflow:
    async def create(self, value):
        return {"id": value["id"]}

    async def get(self, job_id):
        return self

    async def status(self):
        return {"status": "running"}


class Request:
    def __init__(self, path, method="GET", body=None, headers=None):
        self.url = "https://cloud.example" + path
        self.method = method
        self.headers = headers or {}
        self.data = json.dumps(body).encode() if body is not None else b""

    async def bytes(self):
        return self.data


class CloudApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database()
        self.env = SimpleNamespace(DB=self.db, REVIEWS=Workflow(), ADMISSIONS_WEB_PASSWORD="synthetic-test-password",
                                   OPENAI_API_KEY="synthetic-key", OPENAI_MODEL="synthetic-model")
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        response = await self.call("/api/session", "POST", {"password": "synthetic-test-password"})
        self.cookie = response.headers["Set-Cookie"].split(";")[0]

    async def asyncTearDown(self):
        self.db.connection.close()

    async def call(self, path, method="GET", body=None, cookie=True, headers=None):
        value = {"Content-Type": "application/json", **(headers or {})}
        if cookie and hasattr(self, "cookie"):
            value["Cookie"] = self.cookie
        return await self.app.handle(Request(path, method, body, value))

    async def completed_demo(self):
        raw = json.loads((await self.call("/api/demo")).body)
        submitted = await self.call("/api/reviews", "POST", {"mode": "demo", "input": raw})
        self.assertEqual(submitted.status, 202, submitted.body)
        job_id = json.loads(submitted.body)["id"]
        job = await self.app.store.job(job_id)
        result = next_action(json.loads(job["input_json"]), "demo", json.loads(job["config_json"]), {}, ROOT / "examples")
        await self.app.store.write(job_id, {"review/" + k: v for k, v in result["artifacts"].items()}, (result["run"]["status"], "complete"))
        return job_id

    async def test_cloud_auth_csrf_and_persisted_session_survive_new_instance(self):
        self.assertEqual((await self.call("/api/config", cookie=False)).status, 401)
        self.assertEqual((await self.call("/api/reviews", "POST", {}, headers={"Origin": "https://elsewhere.example"})).status, 403)
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        self.assertEqual((await self.call("/api/config")).status, 200)
        self.env.ADMISSIONS_WEB_PASSWORD = "changed-synthetic-password"
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        self.assertEqual((await self.call("/api/config")).status, 401)

    async def test_cloud_job_and_approval_persist_with_atomic_revision_and_receipt(self):
        job_id = await self.completed_demo()
        self.app = CloudApp(self.env, {}, ROOT / "examples")
        response = await self.call("/api/reviews/" + job_id)
        run = json.loads(response.body)["result"]
        body = {"run_sha256": run["run_sha256"], "draft_sha256": run["draft_sha256"], "change_ids": [run["changes"][0]["id"]]}
        approved = await self.call(f"/api/reviews/{job_id}/approve", "POST", body)
        self.assertEqual(approved.status, 201, approved.body)
        urls = json.loads(approved.body)
        text = await self.call(urls["revision"])
        receipt = json.loads((await self.call(urls["receipt"])).body)
        self.assertEqual(receipt["revised_sha256"], sha256(text.body.encode()))
        body["run_sha256"] = "stale"
        self.assertEqual((await self.call(f"/api/reviews/{job_id}/approve", "POST", body)).status, 409)
        self.assertEqual((await self.call(f"/api/reviews/{job_id}/files/../input.json")).status, 404)

    async def test_artifact_batch_rolls_back_on_duplicate_and_stores_large_utf8(self):
        job_id = await self.completed_demo()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.app.store.write(job_id, {"outputs/new.txt": "new", "review/run.json": "duplicate"})
        response = await self.call(f"/api/reviews/{job_id}/files/outputs/new.txt")
        self.assertEqual(response.status, 404)
        large = "合成文本" * 80000
        await self.app.store.write(job_id, {"outputs/large.txt": large})
        self.assertEqual(await self.app.store.read(job_id, "outputs/large.txt"), large)

    async def test_global_job_limit_is_atomic_and_bad_inputs_do_not_create_jobs(self):
        bad = await self.call("/api/reviews", "POST", {"mode": "configured", "input": {"draft": "missing requirements"}})
        self.assertEqual(bad.status, 400)
        demo = json.loads((await self.call("/api/demo")).body)
        results = await asyncio.gather(*(self.call("/api/reviews", "POST", {"mode": "demo", "input": demo}) for _ in range(5)))
        self.assertEqual([r.status for r in results].count(202), 4)
        self.assertEqual([r.status for r in results].count(429), 1)

    async def test_cloud_rejects_oversized_and_non_json_posts_before_task_creation(self):
        oversized = await self.call("/api/reviews", "POST", {}, headers={"Content-Length": "524289"})
        self.assertEqual(oversized.status, 413)
        malformed = await self.call("/api/reviews", "POST", {}, headers={"Content-Length": "invalid"})
        self.assertEqual(malformed.status, 400)
        non_json = await self.call("/api/reviews", "POST", {}, headers={"Content-Type": "text/plain"})
        self.assertEqual(non_json.status, 415)


if __name__ == "__main__":
    unittest.main()
