import copy
import json
import unittest
from pathlib import Path

from admissions_editor.technical import (check_technical_accuracy, extract_technical_context,
                                         normalize_glossary, validate_writing_technical_flags)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class TechnicalTest(unittest.TestCase):
    def context(self, draft, evidence=None, glossary=None):
        return extract_technical_context({"draft": draft, "evidence": evidence or {}}, glossary)

    def checked(self, context, final=None):
        sources = {(s["source_type"], s["source_id"]): s["text"] for s in context["sources"]}
        checks = []
        for claim in context["claims"]:
            if claim["source_type"] == "draft":
                checks.append({"claim_id": claim["id"], "status": "preserved", "original_quote": claim["quote"],
                               "final_quote": claim["quote"], "source_type": claim["source_type"],
                               "source_id": claim["source_id"], "source_quote": claim["quote"],
                               "reason": "Exact technical wording and contribution retained."})
        return checks

    def test_inventory_has_exact_offsets_stable_ids_and_all_categories(self):
        draft = ("I integrated embeddings for retrieval with FAISS into an interface for BERT. "
                 "My teammates fine-tuned the ranking model. We measured MRR 0.42 on the validation set.")
        evidence = {"notes.txt": "The applicant built the interface; the research team fine-tuned BERT."}
        before = copy.deepcopy(evidence)
        context = self.context(draft, evidence)
        self.assertEqual(context, self.context(draft, evidence))
        self.assertEqual(evidence, before)
        self.assertEqual({t["category"] for t in context["terms"]},
                         {"method", "model", "metric", "data_source", "library", "system_component"})
        lookup = {(s["source_type"], s["source_id"]): s["text"] for s in context["sources"]}
        for item in [*context["terms"], *context["claims"]]:
            self.assertEqual(item["quote"], lookup[(item["source_type"], item["source_id"])][item["start"]:item["end"]])
        self.assertTrue(any(c["applicant_contribution"] for c in context["claims"]))
        self.assertTrue(all(c["id"] in context["check_required_claim_ids"] for c in context["claims"] if c["source_type"] == "draft"))

    def test_synthetic_style_cases_preserve_or_block_technical_meaning(self):
        cases = json.loads((EXAMPLES / "technical/cases.json").read_text())["cases"]
        for case in cases:
            with self.subTest(case=case["id"]):
                context = self.context(case["draft"], case.get("evidence"), case.get("glossary"))
                report = check_technical_accuracy(context, case["candidate"])
                self.assertEqual(report["passed"], case["expected_pass"], report["issues"])
                self.assertTrue(set(case["expected_issue_kinds"]).issubset({i["kind"] for i in report["issues"]}))

    def test_both_terms_present_cannot_hide_actor_swapping(self):
        draft = "I built the retrieval component. My teammate built the ranking component."
        candidate = "I built the ranking component. My teammate built the retrieval component."
        report = check_technical_accuracy(self.context(draft), candidate)
        self.assertFalse(report["passed"])
        self.assertIn("contribution_drift", {i["kind"] for i in report["issues"]})
        issue = next(i for i in report["issues"] if i["kind"] == "contribution_drift")
        self.assertEqual(issue["source_quote"], "I built the retrieval component.")

    def test_team_model_training_does_not_authorize_personal_training(self):
        draft = "I built the interface for the model."
        evidence = {"notes.txt": "The research team trained the underlying model."}
        report = check_technical_accuracy(self.context(draft, evidence), "I trained the underlying model.")
        self.assertFalse(report["passed"])
        self.assertIn("contribution_drift", {i["kind"] for i in report["issues"]})

    def test_glossary_aliases_are_guidance_not_factual_sources(self):
        glossary = [{"term": "ShardProbe", "definition": "Our local name for the message replay tool.", "aliases": ["SP"]}]
        context = self.context("I built SP to investigate parser failures.", glossary=glossary)
        self.assertTrue(any(t["canonical"] == "ShardProbe" and t["glossary_term"] == "ShardProbe" for t in context["terms"]))
        self.assertTrue(check_technical_accuracy(context, "I built ShardProbe to investigate parser failures.")["passed"])
        other = self.context("I built a parser.", glossary=glossary)
        self.assertFalse(any(c["quote"] == glossary[0]["definition"] for c in other["claims"]))
        self.assertFalse(check_technical_accuracy(other, "I built ShardProbe and a parser.")["passed"])

    def test_glossary_rejects_bad_sizes_duplicates_and_collapsed_distinctions(self):
        for value in ({}, ["not-an-entry"], [{"term": "x", "definition": ""}],
                      [{"term": "x", "definition": "d", "aliases": ["X"]}],
                      [{"term": "x", "definition": "d"}, {"term": "X", "definition": "e"}],
                      [{"term": "retrieval", "definition": "d", "aliases": ["ranking"]}],
                      [{"term": "fine-tuning", "definition": "d", "aliases": ["embeddings"]}],
                      [{"term": "test data", "definition": "d", "aliases": ["training data"]}],
                      [{"term": "x", "definition": "d", "unknown": True}],
                      [{"term": "x", "definition": "d" * 4001}]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_glossary(value)
        self.assertEqual(normalize_glossary(None), [])

    def test_metric_swap_fails_even_when_number_set_is_unchanged(self):
        draft = "We measured MRR 0.42 and NDCG 0.68 on validation data."
        candidate = "We measured MRR 0.68 and NDCG 0.42 on validation data."
        self.assertEqual(set(__import__('re').findall(r'\d+(?:\.\d+)?', draft)), set(__import__('re').findall(r'\d+(?:\.\d+)?', candidate)))
        report = check_technical_accuracy(self.context(draft), candidate)
        self.assertEqual(len([i for i in report["issues"] if i["kind"] == "metric_drift"]), 2)

    def test_negated_fine_tuning_cannot_become_positive(self):
        draft = "I used embeddings for retrieval; I did not fine-tune the model."
        report = check_technical_accuracy(self.context(draft), draft.replace("did not fine-tune", "fine-tuned"))
        self.assertFalse(report["passed"])
        self.assertIn("negation_drift", {i["kind"] for i in report["issues"]})

    def test_model_checks_must_cover_draft_claims_exactly_and_reference_sources(self):
        draft = "I implemented retrieval with FAISS."
        context = self.context(draft)
        checks = self.checked(context)
        self.assertTrue(check_technical_accuracy(context, draft, checks)["passed"])
        for bad in ([], checks + checks, [{**checks[0], "claim_id": "TECH-invented"}],
                    [{**checks[0], "source_quote": "invented evidence"}],
                    [{**checks[0], "source_type": "glossary"}],
                    [{**checks[0], "original_quote": "retrieval"}],
                    [{**checks[0], "final_quote": "I"}]):
            with self.subTest(checks=bad):
                self.assertFalse(check_technical_accuracy(context, draft, bad)["passed"])

    def test_read_only_model_failure_or_ambiguity_reports_sources(self):
        draft = "I built the retrieval interface."
        context = self.context(draft)
        for status in ("altered", "unsupported", "ambiguous"):
            checks = self.checked(context)
            checks[0].update(status=status, reason="The proposed technical meaning needs applicant verification.")
            report = check_technical_accuracy(context, draft, checks)
            self.assertFalse(report["passed"])
            self.assertEqual(report["issues"][0]["source_quote"], draft)
            self.assertEqual(report["issues"][0]["final_quote"], draft)
            self.assertEqual(draft, "I built the retrieval interface.")

    def test_flagged_writer_changes_must_keep_exact_original_wording(self):
        draft = "I built the interface for the underlying model."
        context = self.context(draft)
        flag = {"claim_id": context["check_required_claim_ids"][0], "reason": "Changing interface to model would expand my contribution."}
        self.assertEqual(validate_writing_technical_flags(context, draft, [flag]), [])
        self.assertTrue(validate_writing_technical_flags(context, "I developed the underlying model.", [flag]))
        self.assertTrue(validate_writing_technical_flags(context, draft, [{**flag, "claim_id": "unknown"}]))

    def test_glossary_and_source_input_never_mutated(self):
        snapshot = {"draft": "I used `ShardProbe` with the parser.", "evidence": {"notes.txt": "ShardProbe is a replay tool."}}
        glossary = [{"term": "ShardProbe", "definition": "A local replay tool."}]
        before = copy.deepcopy((snapshot, glossary))
        context = extract_technical_context(snapshot, glossary)
        check_technical_accuracy(context, snapshot["draft"], self.checked(context))
        self.assertEqual((snapshot, glossary), before)

    def test_claim_identity_is_stable_when_glossary_adds_annotations(self):
        draft = "I built the replay tool to investigate parser failures."
        original = self.context(draft)
        annotated = self.context(draft, glossary=[{"term": "replay tool", "definition": "Project-specific playback component."}])
        self.assertEqual(original["check_required_claim_ids"], annotated["check_required_claim_ids"])
        self.assertNotEqual(original["terms"], annotated["terms"])

    def test_non_technical_draft_needs_no_extra_model_claim_checks(self):
        draft = "I learned to ask better questions and listen to my classmates."
        context = self.context(draft)
        self.assertEqual(context["check_required_claim_ids"], [])
        self.assertTrue(check_technical_accuracy(context, draft, [])["passed"])


if __name__ == "__main__":
    unittest.main()
