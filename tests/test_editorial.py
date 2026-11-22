"""Synthetic checks for report consolidation and global-feedback authority."""

import copy
import json
import unittest
from pathlib import Path

from admissions_editor.cloud_runtime import (next_action, next_feedback_revision_action,
    prepare_input, revision_demo_payload)
from admissions_editor.editorial import (SKILL_IDS, build_editorial_report, demo_feedback,
    answers_from_excerpts, findings_for_report, offline_report_output, prepare_feedback_plan, validate_editorial_context,
    validate_editorial_report, validate_feedback_request)
from admissions_editor.providers import ROLE_NAMES
from admissions_editor.review import sha256
from admissions_editor.revision import json_bytes, verify_revision
from admissions_editor.webinput import WebError
from test_cloud_runtime import CONFIG, response_for
from test_revision import make_source

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class Model:
    def __init__(self, result):
        self.result, self.calls = result, []

    def task(self, task, payload, schema):
        self.calls.append((task, copy.deepcopy(payload), schema))
        return copy.deepcopy(self.result)


def saved_response(value):
    return {"response": {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": json.dumps(value)}]}]}, "attempts": 1}


class EditorialTest(unittest.TestCase):
    def setUp(self):
        self.run, self.options = make_source()
        self.sha = sha256(json_bytes(self.run))
        self.report = build_editorial_report(self.run, self.sha)
        self.demo = revision_demo_payload(EXAMPLES)

    def body(self):
        return {"run_sha256": self.sha, "editorial_report_sha256": self.report["report_sha256"],
                "feedback": self.demo["feedback"], **{key: copy.deepcopy(self.options[key]) for key in ("confirmed_facts",)}}

    def plan(self):
        body = validate_feedback_request(json_bytes(self.run).decode(), self.report, self.body())
        return prepare_feedback_plan(self.run, self.sha, self.report, body, demo_manifest=self.demo)

    def test_numbered_report_groups_and_covers_each_original_finding_once(self):
        self.assertEqual([row["number"] for row in self.report["issues"]], list(range(1, len(self.report["issues"]) + 1)))
        flattened = [fid for row in self.report["issues"] for fid in row["finding_ids"]]
        self.assertEqual(sorted(flattened), sorted(row["finding_id"] for row in findings_for_report(self.run)))
        self.assertLess(len(self.report["issues"]), len(flattened))
        self.assertEqual(self.report["generation_source"], "offline_summary")
        validate_editorial_report(self.run, self.sha, self.report)

    def test_model_report_uses_stable_frozen_content_and_reports_source(self):
        provider = Model(offline_report_output(findings_for_report(self.run)))
        report = build_editorial_report(self.run, self.sha, provider)
        self.assertEqual(report["generation_source"], "model")
        payload = provider.calls[0][1]
        self.assertNotIn("created_at_utc", payload)
        self.assertNotIn("review_sha256", payload)
        self.assertEqual(payload["original"], self.run["snapshot"])

    def test_bad_model_report_falls_back_explicitly_without_laundering_missing_findings(self):
        provider = Model({"summary": "bad", "issues": []})
        report = build_editorial_report(self.run, self.sha, provider)
        self.assertEqual(report["generation_source"], "model_synthesis_failed")
        self.assertTrue(report["diagnostics"])
        validate_editorial_report(self.run, self.sha, report)

    def test_stale_report_run_or_feedback_and_unconfirmed_material_rejected_before_model(self):
        for field in ("run_sha256", "editorial_report_sha256"):
            body = self.body()
            body[field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(WebError) as caught:
                validate_feedback_request(json_bytes(self.run).decode(), self.report, body)
            self.assertEqual(caught.exception.status, 409)
        for feedback in ("", " ", "x\0y", "x" * 32769):
            body = self.body()
            body["feedback"] = feedback
            with self.assertRaises(WebError):
                validate_feedback_request(json_bytes(self.run).decode(), self.report, body)
        body = self.body()
        body["confirmed_facts"] = [{"id": "RAW", "text": "UNCONFIRMED-NOT-FOR-MODEL", "confirmed": False}]
        with self.assertRaises(WebError) as caught:
            validate_feedback_request(json_bytes(self.run).decode(), self.report, body)
        self.assertEqual(caught.exception.status, 400)

    def test_global_comment_builds_same_known_decisions_without_user_per_finding_input(self):
        plan = self.plan()
        context = plan["editorial_context"]
        self.assertEqual(context["feedback"], self.demo["feedback"])
        self.assertEqual(context["selected_skill_ids"], list(SKILL_IDS))
        self.assertEqual(plan["answers"], self.options["answers"])
        self.assertEqual({r["finding_id"]: r["decision"] for r in plan["findings"]},
                         {r["finding_id"]: r["decision"] for r in self.options["decisions"]})

    def test_missing_or_unknown_model_decisions_and_skills_are_rejected(self):
        base = self.plan()["editorial_context"]["interpretation"]
        for mutation in ("missing", "unknown", "skill"):
            output = copy.deepcopy(base)
            if mutation == "missing":
                output["decisions"].pop()
            elif mutation == "unknown":
                output["decisions"][0]["finding_id"] = "made-up"
            else:
                output["selected_skill_ids"] = ["download-untrusted-skill"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                prepare_feedback_plan(self.run, self.sha, self.report, self.body(), Model(output))

    def test_hypothesis_or_instruction_cannot_be_confirmed_by_quoting_just_affirmative_substring(self):
        context = self.plan()["editorial_context"]
        finding = context["interpretation"]["answer_excerpts"][0]["finding_id"]
        quote = "I trained the model on 999 records"
        for feedback in ("If " + quote + ", it would sound stronger.", "例如：" + quote, "不要写 " + quote,
                         "Suppose:\n" + quote, "Please improve the essay. For example:\n\n" + quote,
                         "请调整语气。假设：\n" + quote):
            changed = copy.deepcopy(context)
            changed.update(feedback=feedback, feedback_sha256=sha256(feedback.encode()))
            changed["interpretation"]["answer_excerpts"] = [{"finding_id": finding, "quote": quote}]
            with self.subTest(feedback=feedback), self.assertRaises(ValueError):
                validate_editorial_context(changed, self.sha)

    def test_explicit_confirmation_header_after_editorial_instruction_keeps_first_fact_usable(self):
        context = self.plan()["editorial_context"]
        finding = context["interpretation"]["answer_excerpts"][0]["finding_id"]
        quote = self.options["confirmed_facts"][0]["text"]
        for header in ("请根据综合建议优化整篇文章，保留真实经历、技术含义和我的语气。以下是我确认的信息：",
                       "Please improve the essay. Confirmed facts:"):
            changed = copy.deepcopy(context)
            feedback = header + "\n" + quote
            changed.update(feedback=feedback, feedback_sha256=sha256(feedback.encode()))
            changed["interpretation"]["answer_excerpts"] = [{"finding_id": finding, "quote": quote}]
            with self.subTest(header=header):
                validate_editorial_context(changed, self.sha)

    def test_confirmation_header_does_not_turn_future_or_desired_contributions_into_facts(self):
        context = self.plan()["editorial_context"]
        finding = context["interpretation"]["answer_excerpts"][0]["finding_id"]
        for quote in ("I want to train the model.", "I plan to build the interface.", "I could train the model.",
                      "I will train the model.", "My goal is to build the model.", "I hope to study data science and win an award.",
                      "我希望负责模型训练。", "我打算独立开发底层模型。", "我将独立构建底层模型。"):
            changed = copy.deepcopy(context)
            feedback = "请润色文章。以下是我确认的信息：\n" + quote
            changed.update(feedback=feedback, feedback_sha256=sha256(feedback.encode()))
            changed["interpretation"]["answer_excerpts"] = [{"finding_id": finding, "quote": quote}]
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                validate_editorial_context(changed, self.sha)

    def test_distinct_quotes_can_answer_the_same_finding_and_keep_goals_as_goals(self):
        base = self.plan()["editorial_context"]["interpretation"]
        facts = self.options["confirmed_facts"]
        output = copy.deepcopy(base)
        output["answer_excerpts"] = [
            {"finding_id": "skeptical-01", "quote": facts[0]["text"]},
            {"finding_id": "skeptical-01", "quote": facts[1]["text"]},
            {"finding_id": "admissions-01", "quote": facts[2]["text"]},
            {"finding_id": "admissions-01", "quote": facts[3]["text"]},
        ]
        plan = prepare_feedback_plan(self.run, self.sha, self.report, self.body(), Model(output))
        self.assertEqual(plan["answers"]["skeptical-01"], facts[0]["text"] + "\n" + facts[1]["text"])
        self.assertEqual(plan["answers"]["admissions-01"], facts[2]["text"] + "\n" + facts[3]["text"])
        self.assertEqual(plan["editorial_context"]["interpretation"]["answer_excerpts"], output["answer_excerpts"])

    def test_duplicate_nested_and_overlapping_excerpts_do_not_inflate_one_answer(self):
        text = "I built the parser and tested the interface."
        for quotes in ((text, text), (text, "I built the parser"), ("I built the parser and", "parser and tested the interface.")):
            rows = [{"finding_id": "skeptical-01", "quote": quote} for quote in quotes]
            with self.subTest(quotes=quotes), self.assertRaises(ValueError):
                answers_from_excerpts(rows, text)

    def test_same_factual_source_can_answer_multiple_findings(self):
        quote = self.options["confirmed_facts"][0]["text"]
        rows = [{"finding_id": fid, "quote": quote} for fid in ("skeptical-01", "admissions-01")]
        self.assertEqual(answers_from_excerpts(rows, quote), {row["finding_id"]: quote for row in rows})

    def test_only_explicit_educational_intent_is_authorized_by_goal_wording(self):
        context = self.plan()["editorial_context"]
        for quote in ("I hope to explore retrieval and ranking.", "My goal is to study distributed systems.", "我希望学习分布式系统。"):
            changed = copy.deepcopy(context)
            feedback = "以下是我确认的信息：\n" + quote
            changed.update(feedback=feedback, feedback_sha256=sha256(feedback.encode()))
            changed["interpretation"]["answer_excerpts"] = [{"finding_id": "admissions-01", "quote": quote}]
            with self.subTest(quote=quote):
                validate_editorial_context(changed, self.sha)

    def test_empty_summary_is_rejected_when_review_has_no_findings(self):
        from admissions_editor.editorial import _validate_report_output
        with self.assertRaises(ValueError):
            _validate_report_output({"summary": " ", "issues": []}, [])

    def test_durable_model_report_resumes_without_repeating_reviewers_or_timestamp_requests(self):
        config = {**copy.deepcopy(CONFIG), "editorial_reports": True}
        responses, calls = {}, []
        inputs = prepare_input(self.demo["input"], "demo", EXAMPLES)
        for _ in range(8):
            action = next_action(inputs, "configured", config, responses, EXAMPLES)
            if action["complete"]:
                break
            calls.append(action["call_id"])
            if action["call_id"] == "editorial_report":
                payload = json.loads(action["payload"]["input"])
                repeated = next_action(inputs, "configured", config, responses, EXAMPLES)
                self.assertEqual(action["key"], repeated["key"])
                responses[action["key"]] = saved_response(offline_report_output(payload["findings"]))
            else:
                responses[action["key"]] = response_for(action)
        self.assertTrue(action["complete"])
        self.assertEqual(calls, [*ROLE_NAMES, "observe", "decide", "editorial_report"])
        report = json.loads(action["artifacts"]["editorial-report.json"])
        validate_editorial_report(action["run"], sha256(action["artifacts"]["run.json"].encode()), report)

    def test_durable_feedback_model_precedes_existing_four_stages(self):
        output = self.plan()["editorial_context"]["interpretation"]
        responses, calls = {}, []
        config = {"revision": {stage: {"model": "synthetic", "retries": 0} for stage in (*SKILL_IDS, "verify_revision")}}
        body = validate_feedback_request(json_bytes(self.run).decode(), self.report, self.body())
        for _ in range(6):
            action = next_feedback_revision_action(self.run, self.sha, self.report, body, config, responses, EXAMPLES)
            if action["complete"]:
                break
            calls.append(action["call_id"])
            repeated = next_feedback_revision_action(self.run, self.sha, self.report, body, config, responses, EXAMPLES)
            self.assertEqual(action["key"], repeated["key"])
            value = output if action["call_id"] == "feedback_plan" else json.loads(
                (EXAMPLES / "revision/fixtures" / (action["call_id"] + ".json")).read_text())
            responses[action["key"]] = saved_response(value)
        self.assertTrue(action["complete"])
        self.assertEqual(calls, ["feedback_plan", *SKILL_IDS, "verify_revision"])
        self.assertEqual(action["record"]["status"], "ready", action["record"]["validation"])
        self.assertIn("feedback-analysis.json", action["artifacts"])
        self.assertTrue(verify_revision(action["record"]))


if __name__ == "__main__":
    unittest.main()
