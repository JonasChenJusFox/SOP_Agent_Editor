"""Actual loopback HTTP and SQLite-backed D1 tests for the simplified flow."""

import copy
import io
import json
import unittest
import zipfile
from pathlib import Path

from admissions_editor.cloud_runtime import next_feedback_revision_action
from admissions_editor.review import sha256
from admissions_editor.webserver import WebApp
from admissions_editor.word_documents import DOCX_MIME
import test_revision_api as helpers

ROOT = Path(__file__).resolve().parents[1]


def body_for(demo, review):
    return {"run_sha256": review["result"]["run_sha256"],
            "editorial_report_sha256": review["result"]["editorial_report"]["report_sha256"],
            "feedback": demo["feedback"], "confirmed_facts": copy.deepcopy(demo["plan_input"]["confirmed_facts"])}


def assert_docx(test, content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        test.assertIsNone(archive.testzip())
        test.assertIn("word/document.xml", archive.namelist())
        test.assertTrue(archive.read("word/document.xml"))


class LocalEditorialApiTest(unittest.TestCase):
    # Borrow transport helpers without inheriting/repeating the old test cases.
    setUp = helpers.LocalRevisionApiTest.setUp
    close = helpers.LocalRevisionApiTest.close
    request = helpers.LocalRevisionApiTest.request
    json_request = helpers.LocalRevisionApiTest.json_request
    completed_job = helpers.LocalRevisionApiTest.completed_job
    completed_review = helpers.LocalRevisionApiTest.completed_review

    def test_one_comment_generates_two_word_documents_and_restorable_analysis(self):
        demo, review = self.completed_review()
        source = self.request(f"/api/reviews/{review['id']}/files/review/run.json")[2]
        report_url = f"/api/reviews/{review['id']}/documents/editorial-report.docx"
        status, headers, document = self.request(report_url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        assert_docx(self, document)
        submitted = self.json_request(f"/api/reviews/{review['id']}/feedback-revisions", body_for(demo, review), 202)
        revision = self.completed_job(submitted["id"], "revisions")
        self.assertEqual(revision["result"]["status"], "ready", revision)
        self.assertEqual(revision["result"]["feedback"], demo["feedback"])
        for name in ("essay.docx", "change-report.docx"):
            status, headers, document = self.request(f"/api/revisions/{revision['id']}/documents/{name}")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], DOCX_MIME)
            self.assertIn(name, headers["Content-Disposition"])
            assert_docx(self, document)
        self.assertEqual(self.request(f"/api/reviews/{review['id']}/files/review/run.json")[2], source)
        self.assertEqual(self.json_request(f"/api/revisions/{revision['id']}")["result"]["feedback"], demo["feedback"])

    def test_stale_feedback_and_auth_errors_do_not_create_jobs_or_expose_documents(self):
        demo, review = self.completed_review()
        before = len(self.app.jobs)
        body = body_for(demo, review)
        body["editorial_report_sha256"] = "0" * 64
        self.assertEqual(self.request(f"/api/reviews/{review['id']}/feedback-revisions", body)[0], 409)
        self.assertEqual(len(self.app.jobs), before)
        self.app.password = "synthetic-editorial-password"
        for path, body in ((f"/api/reviews/{review['id']}/feedback-revisions", body_for(demo, review)),
                           (f"/api/reviews/{review['id']}/documents/editorial-report.docx", None)):
            self.assertEqual(self.request(path, body)[0], 401)

    def test_edited_offline_feedback_is_explicitly_deferred_but_candidate_word_remains_available(self):
        demo, review = self.completed_review()
        body = body_for(demo, review)
        body["feedback"] = "请将第二个项目重写，但我还没有提供个人分工。"
        submitted = self.json_request(f"/api/reviews/{review['id']}/feedback-revisions", body, 202)
        revision = self.completed_job(submitted["id"], "revisions")
        self.assertEqual(revision["result"]["status"], "blocked")
        self.assertNotIn("final_text", revision["result"])
        status, _, document = self.request(f"/api/revisions/{revision['id']}/documents/essay.docx")
        self.assertEqual(status, 200)
        assert_docx(self, document)
        with zipfile.ZipFile(io.BytesIO(document)) as archive:
            self.assertIn("候选", archive.read("word/document.xml").decode())


class CloudEditorialApiTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.CloudRevisionApiTest.asyncSetUp
    asyncTearDown = helpers.CloudRevisionApiTest.asyncTearDown
    call = helpers.CloudRevisionApiTest.call
    json_call = helpers.CloudRevisionApiTest.json_call
    completed_review = helpers.CloudRevisionApiTest.completed_review

    async def finish_feedback(self, demo, review, body=None):
        submitted = await self.json_call(f"/api/reviews/{review['id']}/feedback-revisions", "POST", body or body_for(demo, review), 202)
        row = await self.app.store.job(submitted["id"])
        source_text = await self.app.store.read(review["id"], "review/run.json")
        inputs = json.loads(row["input_json"])
        action = next_feedback_revision_action(json.loads(source_text), sha256(source_text.encode()),
            inputs["editorial_report"], inputs["feedback_request"], json.loads(row["config_json"]), {},
            ROOT / "examples", row["mode"], inputs.get("knowledge_context"))
        self.assertTrue(action["complete"])
        ready = action["record"]["status"] == "ready"
        await self.app.store.write(row["id"], {"revision/" + key: value for key, value in action["artifacts"].items()},
                                   ("complete" if ready else "partial", "完成"))
        return await self.json_call(f"/api/revisions/{row['id']}")

    async def test_cloud_feedback_plan_and_binary_word_documents_persist(self):
        demo, review = await self.completed_review()
        revision = await self.finish_feedback(demo, review)
        self.assertEqual(revision["result"]["status"], "ready", revision)
        self.assertEqual(revision["result"]["feedback"], demo["feedback"])
        for route in (f"/api/reviews/{review['id']}/documents/editorial-report.docx",
                      f"/api/revisions/{revision['id']}/documents/essay.docx",
                      f"/api/revisions/{revision['id']}/documents/change-report.docx"):
            response = await self.call(route)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], DOCX_MIME)
            self.assertIsInstance(response.body, bytes)
            assert_docx(self, response.body)
            self.assertEqual((await self.call(route, cookie=False)).status, 401)
        restored = helpers.CloudApp(self.env, {}, ROOT / "examples")
        self.app = restored
        result = await self.json_call(f"/api/revisions/{revision['id']}")
        self.assertEqual(result["result"]["feedback"], demo["feedback"])

    async def test_cloud_old_reviews_have_bound_fallback_reports_and_stale_comments_reject(self):
        demo, review = await self.completed_review()
        await self.app.store.sql("DELETE FROM files WHERE job_id=? AND path='review/editorial-report.json'", review["id"]).run()
        restored = await self.json_call(f"/api/reviews/{review['id']}")
        self.assertEqual(restored["result"]["editorial_report"]["generation_source"], "offline_summary")
        body = body_for(demo, restored)
        body["run_sha256"] = "0" * 64
        response = await self.call(f"/api/reviews/{review['id']}/feedback-revisions", "POST", body)
        self.assertEqual(response.status, 409)
        self.assertEqual((await self.call(f"/api/reviews/{review['id']}/feedback-revisions", "POST", body, cookie=False)).status, 401)


if __name__ == "__main__":
    unittest.main()
