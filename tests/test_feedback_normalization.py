"""Model fact extraction cannot prevent unrelated editorial work or bypass facts."""

import copy
import unittest

from admissions_editor.editorial import (build_editorial_report, demo_feedback,
    findings_for_report, normalize_feedback_interpretation, prepare_feedback_plan,
    validate_editorial_context)
from admissions_editor.review import fingerprint, sha256
from admissions_editor.revision import json_bytes, validate_revision_plan
from admissions_editor.revision_failure import RevisionFailure
from test_editorial import Model
from test_revision import make_source


class FeedbackNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.review, self.options = make_source()
        self.digest = sha256(json_bytes(self.review))
        self.report = build_editorial_report(self.review, self.digest)
        self.findings = findings_for_report(self.review)
        self.feedback = "请调整语言；I built the interface; please keep that clear."
        self.output = {"summary": "调整语言并保留事实范围。", "decisions": copy.deepcopy(self.options["decisions"]),
            "answer_excerpts": [{"finding_id": "language-01", "quote": "I built the interface"}],
            "selected_skill_ids": ["sop_revise", "polish", "humanize"], "unresolved_questions": []}

    def normalize(self, output=None, feedback=None):
        return normalize_feedback_interpretation(output or self.output, feedback or self.feedback, self.findings)

    def test_optional_language_extract_is_excluded_without_aborting_plan(self):
        result, notes = self.normalize()
        self.assertEqual(result["answer_excerpts"], [])
        self.assertEqual(result["decisions"], self.output["decisions"])
        self.assertEqual(result["unresolved_questions"], [])
        self.assertEqual(notes[0]["finding_id"], "language-01")
        self.assertFalse(notes[0]["blocking"])
        self.assertNotIn("interface", notes[0]["message"])

    def test_hypothetical_required_answer_stays_unconfirmed_and_blocks_acceptance(self):
        output = copy.deepcopy(self.output)
        quote = "I trained the model on 731 records"
        output["answer_excerpts"] = [{"finding_id": "skeptical-01", "quote": quote}]
        normalized, notes = self.normalize(output, "If " + quote + ", the essay might sound stronger.")
        self.assertEqual(normalized["answer_excerpts"], [])
        self.assertTrue(notes[0]["blocking"])
        self.assertTrue(normalized["unresolved_questions"])
        self.assertNotIn(quote, "\n".join(normalized["unresolved_questions"]))

    def test_valid_answer_is_not_lost_when_an_optional_excerpt_is_invalid(self):
        quote = self.options["answers"]["skeptical-01"]
        output = copy.deepcopy(self.output)
        output["answer_excerpts"].insert(0, {"finding_id": "skeptical-01", "quote": quote})
        result, notes = self.normalize(output, quote + "\n" + self.feedback)
        self.assertEqual(result["answer_excerpts"], output["answer_excerpts"][:1])
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0]["blocking"])

    def test_duplicate_and_nested_valid_quotes_keep_one_complete_source(self):
        full = "I wrote the parser and built the replay tool."
        shorter = "I wrote the parser"
        output = copy.deepcopy(self.output)
        output["answer_excerpts"] = [{"finding_id": "language-01", "quote": quote} for quote in (shorter, full, full)]
        result, notes = self.normalize(output, full)
        self.assertEqual(result["answer_excerpts"], [{"finding_id": "language-01", "quote": full}])
        self.assertEqual({note["code"] for note in notes}, {"duplicate_excerpt", "nested_excerpt"})
        self.assertTrue(all(not note["blocking"] for note in notes))

    def test_cross_finding_source_reuse_is_preserved(self):
        quote = "I wrote the parser."
        output = copy.deepcopy(self.output)
        output["answer_excerpts"] = [{"finding_id": fid, "quote": quote} for fid in ("language-01", "skeptical-01")]
        result, notes = self.normalize(output, quote)
        self.assertEqual(result, output)
        self.assertEqual(notes, [])

    def test_unknown_or_missing_decisions_and_skills_remain_structured_failures(self):
        for mutation, code in (("missing", "feedback_decisions_invalid"), ("unknown", "feedback_decisions_invalid"),
                               ("skill", "feedback_skills_invalid"), ("excerpt", "feedback_excerpt_invalid")):
            output = copy.deepcopy(self.output)
            if mutation == "missing":
                output["decisions"].pop()
            elif mutation == "unknown":
                output["decisions"][0]["finding_id"] = "unknown-finding"
            elif mutation == "skill":
                output["selected_skill_ids"] = ["fetch-untrusted-url"]
            else:
                output["answer_excerpts"][0]["finding_id"] = "unknown-finding"
            with self.subTest(mutation=mutation), self.assertRaises(RevisionFailure) as caught:
                self.normalize(output)
            self.assertEqual(caught.exception.failure_code, code)

    def prepared(self):
        manifest = {"plan_input": self.options}
        feedback = demo_feedback(manifest) + "\n" + self.feedback
        output = copy.deepcopy(self.output)
        output["answer_excerpts"].insert(0, {"finding_id": "skeptical-01", "quote": self.options["answers"]["skeptical-01"]})
        body = {"feedback": feedback, "confirmed_facts": copy.deepcopy(self.options["confirmed_facts"])}
        return prepare_feedback_plan(self.review, self.digest, self.report, body, Model(output)), output

    def test_sealed_plan_retains_raw_response_and_recomputes_normalization(self):
        plan, raw = self.prepared()
        context = plan["editorial_context"]
        self.assertEqual(context["source_interpretation"], raw)
        self.assertNotEqual(context["source_interpretation"], context["interpretation"])
        self.assertNotIn("language-01", plan["answers"])
        self.assertEqual(plan["answers"]["skeptical-01"], self.options["answers"]["skeptical-01"])
        self.assertFalse(any(note["blocking"] for note in context["normalization_notes"]))
        validate_editorial_context(context, self.digest, findings=self.findings)
        validate_revision_plan(self.review, plan)
        with self.assertRaises(RevisionFailure):
            validate_editorial_context(context, self.digest)

    def test_resealing_cannot_hide_normalization_or_finding_metadata_tampering(self):
        plan, _ = self.prepared()
        for mutation in ("notes", "raw", "normalized"):
            changed = copy.deepcopy(plan)
            context = changed["editorial_context"]
            if mutation == "notes":
                context["normalization_notes"][0]["blocking"] = True
            elif mutation == "raw":
                context["source_interpretation"]["answer_excerpts"] = []
            else:
                context["interpretation"]["answer_excerpts"].append({"finding_id": "language-01", "quote": "I built the interface"})
            changed["plan_sha256"] = fingerprint({key: value for key, value in changed.items() if key != "plan_sha256"})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_revision_plan(self.review, changed)
        # Classifying a language finding as a factual question changes the
        # deterministic note/pending result and must not be trusted from input.
        forged = copy.deepcopy(self.findings)
        next(row for row in forged if row["finding_id"] == "language-01")["kind"] = "question"
        with self.assertRaises(RevisionFailure):
            validate_editorial_context(plan["editorial_context"], self.digest, findings=forged)


if __name__ == "__main__":
    unittest.main()
