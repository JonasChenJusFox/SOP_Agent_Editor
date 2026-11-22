"""Confirmed applicant facts ground technical edits without widening their scope."""

import copy
import unittest

from admissions_editor.review import fingerprint, run_review, sha256
from admissions_editor.revision import (json_bytes, prepare_revision_plan, revise_document,
                                       validate_revision_plan, verify_revision)
from admissions_editor.technical import check_technical_accuracy, extract_technical_context
from admissions_editor.workflow import RuleWorkflowProvider, enrich_run
from test_revision import make_source
from test_technical_integration import ClearReviewer, TechnicalEditor


def source(draft="I built an interface.", requirements="Describe your actual contribution. Maximum 500 words."):
    run = run_review(ClearReviewer(), draft.encode(), requirements, "Preserve factual scope.", {})
    return enrich_run(run, RuleWorkflowProvider())


def fact(text, *, confirmed=True, fact_id="FACT-BACKEND"):
    return {"id": fact_id, "text": text, "confirmed": confirmed, "kind": "personal_contribution"}


class ConfirmedTechnicalSourcesTest(unittest.TestCase):
    def plan(self, run=None, **kwargs):
        run = run or source()
        return prepare_revision_plan(run, sha256(json_bytes(run)), **kwargs)

    def test_confirmed_facts_and_answers_are_exact_sources_with_declared_authority(self):
        run, options = make_source()
        options["answers"]["skeptical-01"] = "I implemented a retrieval backend."
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), **options)
        sources = {item["source_id"]: item for item in plan["technical_context"]["sources"] if item["source_type"] == "confirmed_fact"}
        self.assertEqual(sources["ANSWER-skeptical-01"]["text"], options["answers"]["skeptical-01"])
        self.assertEqual(sources["ANSWER-skeptical-01"]["authority"], "author_confirmation")
        self.assertTrue(set(plan["technical_context"]["check_required_claim_ids"]).issubset(
            {claim["id"] for claim in plan["technical_context"]["claims"] if claim["source_type"] == "draft"}))
        candidate = run["snapshot"]["draft"] + "\n\nI implemented a retrieval backend."
        report = check_technical_accuracy(plan["technical_context"], candidate, used_fact_ids=["ANSWER-skeptical-01"])
        self.assertTrue(report["passed"], report)

    def test_confirmed_backend_retrieval_sources_allow_only_reported_facts(self):
        plan = self.plan(confirmed_facts=[fact("I implemented a retrieval backend.")])
        candidate = "I built an interface and implemented a retrieval backend."
        accepted = check_technical_accuracy(plan["technical_context"], candidate, used_fact_ids=["FACT-BACKEND"])
        self.assertTrue(accepted["passed"], accepted)
        omitted = check_technical_accuracy(plan["technical_context"], candidate, used_fact_ids=[])
        self.assertFalse(omitted["passed"])
        self.assertTrue(any(issue["kind"] == "unreported_confirmed_fact" for issue in omitted["issues"]))

    def test_unconfirmed_prose_never_enters_technical_context(self):
        plan = self.plan(confirmed_facts=[fact("PRIVATE_UNCONFIRMED_SENTINEL: I implemented a retrieval backend.", confirmed=False)])
        self.assertNotIn("PRIVATE_UNCONFIRMED_SENTINEL", str(plan["technical_context"]))
        report = check_technical_accuracy(plan["technical_context"], "I built an interface and implemented a retrieval backend.")
        self.assertFalse(report["passed"])

    def test_requirements_are_wording_context_not_applicant_factual_authority(self):
        requirements = "For the SM in Data Science, discuss PyTorch, backend development, retrieval and training a BERT model."
        plan = self.plan(source(requirements=requirements))
        context = plan["technical_context"]
        self.assertEqual(context["terminology_context"]["requirements"], requirements)
        self.assertTrue(all(source["text"] != requirements for source in context["sources"]))
        for addition in (" I implemented a retrieval backend.", " I trained BERT with PyTorch."):
            with self.subTest(addition=addition):
                report = check_technical_accuracy(context, "I built an interface." + addition)
                self.assertFalse(report["passed"])

    def test_voice_and_decision_reasons_are_not_technical_fact_sources(self):
        run, options = make_source()
        for decision in options["decisions"]:
            decision["reason"] += " I trained BERT and built a retrieval backend."
        options["writing_sample"] = "I trained BERT and built a retrieval backend."
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), **options)
        self.assertTrue(all("trained BERT" not in source["text"] for source in plan["technical_context"]["sources"]))
        report = check_technical_accuracy(plan["technical_context"], run["snapshot"]["draft"] + " I trained BERT.")
        self.assertFalse(report["passed"])

    def test_degree_acronyms_are_contextual_and_custom_project_acronyms_still_checked(self):
        context = extract_technical_context({"draft": "I built an interface.", "evidence": {}})
        for degree in ("the SM in Data Science", "the S.M. in Data Science", "the MS in Computer Science", "the M.Sc. in Statistics"):
            candidate = "I built an interface. I am applying to " + degree + "."
            with self.subTest(degree=degree):
                report = check_technical_accuracy(context, candidate)
                self.assertTrue(report["passed"], report)
        self.assertFalse(check_technical_accuracy(context, "I built an interface and an SM model.")["passed"])
        project_context = extract_technical_context({"draft": "I built an interface.", "evidence": {}},
            [{"term": "SM", "definition": "The project's synthetic state machine.", "aliases": []}])
        report = check_technical_accuracy(project_context, "I built an interface and an SM controller.")
        self.assertFalse(report["passed"])
        self.assertTrue(any("SM" in issue["reason"] for issue in report["issues"]))

    def test_specific_frontend_paraphrases_preserve_scope_without_licensing_models(self):
        pairs = (("I developed a user-facing application.", "I built the interface."),
                 ("I implemented a front end.", "I built a frontend interface."),
                 ("I built an interface for the ranking model.", "I built the ranking model interface."))
        for original, candidate in pairs:
            with self.subTest(original=original):
                context = extract_technical_context({"draft": original, "evidence": {}})
                report = check_technical_accuracy(context, candidate)
                self.assertTrue(report["passed"], report)
        generic = extract_technical_context({"draft": "I developed an application.", "evidence": {}})
        self.assertFalse(check_technical_accuracy(generic, "I built the interface.")["passed"])

    def test_coordinated_component_objects_preserve_their_actual_actions(self):
        context = extract_technical_context({"draft": "My teammates built the model.", "evidence": {}}, confirmed_facts=[
            fact("I built the backend and the user-facing application.")])
        report = check_technical_accuracy(context,
            "My teammates built the model. I developed the interface and backend.", used_fact_ids=["FACT-BACKEND"])
        self.assertTrue(report["passed"], report)
        action_context = extract_technical_context({"draft": "My teammates built the model.", "evidence": {}}, confirmed_facts=[
            fact("I built the interface and evaluated the model.")])
        report = check_technical_accuracy(action_context,
            "My teammates built the model. I developed the interface and model.", used_fact_ids=["FACT-BACKEND"])
        self.assertFalse(report["passed"])
        self.assertTrue(any(issue["kind"] == "contribution_drift" for issue in report["issues"]))

    def test_handled_components_do_not_imply_built_or_trained_models(self):
        context = extract_technical_context({"draft": "My teammates built the model.", "evidence": {}}, confirmed_facts=[
            fact("I handled the backend and built the user-facing application.")])
        report = check_technical_accuracy(context,
            "My teammates built the model. I handled the backend and developed the interface.", used_fact_ids=["FACT-BACKEND"])
        self.assertTrue(report["passed"], report)
        for addition in ("I developed the backend.", "I trained the model."):
            with self.subTest(addition=addition):
                self.assertFalse(check_technical_accuracy(context,
                    "My teammates built the model. I built the interface. " + addition,
                    used_fact_ids=["FACT-BACKEND"])["passed"])

    def test_affirmative_preposed_component_action_retains_its_subject(self):
        for introducer in ("While", "When", "By", "After", "Through", "In"):
            with self.subTest(introducer=introducer):
                context = extract_technical_context({"draft": "My teammates built the model.", "evidence": {}}, confirmed_facts=[
                    fact(introducer + " building the user-facing application and working with the backend, I learned to test carefully.")])
                report = check_technical_accuracy(context,
                    "My teammates built the model. I built the interface and worked with the backend.", used_fact_ids=["FACT-BACKEND"])
                self.assertTrue(report["passed"], report)
                self.assertFalse(check_technical_accuracy(context,
                    "My teammates built the model. I trained the model.", used_fact_ids=["FACT-BACKEND"])["passed"])

    def test_neutral_answer_label_preserves_exact_source_without_hiding_its_actor(self):
        for label in ("参考：", "补充：", "Answer: "):
            with self.subTest(label=label):
                text = label + "While building the user-facing interface and working with the backend I handled the test cases."
                context = extract_technical_context({"draft": "My teammates built the model.", "evidence": {}}, confirmed_facts=[fact(text)])
                self.assertEqual(next(source["text"] for source in context["sources"] if source["source_type"] == "confirmed_fact"), text)
                report = check_technical_accuracy(context,
                    "My teammates built the model. I developed the interface and worked with the backend.", used_fact_ids=["FACT-BACKEND"])
                self.assertTrue(report["passed"], report)

    def test_hypothetical_negated_and_other_actor_prefixes_do_not_authorize_work(self):
        for declaration in ("Before building the interface, I tested the backend.",
                            "Without building the interface, I tested the backend.",
                            "Instead of building the interface, I tested the backend.",
                            "If building the interface helped, I would test the backend.",
                            "While building the interface, I might test the backend.",
                            "队友：While building the interface, I tested the backend.",
                            "Teammate: While building the interface, I tested the backend.",
                            "When building the interface, my teammates tested it and I observed the backend."):
            with self.subTest(declaration=declaration):
                context = extract_technical_context({"draft": "My teammates built the model.", "evidence": {}}, confirmed_facts=[fact(declaration)])
                report = check_technical_accuracy(context,
                    "My teammates built the model. I built the interface.", used_fact_ids=["FACT-BACKEND"])
                self.assertFalse(report["passed"])

    def test_confirmed_interface_fact_does_not_authorize_model_training_or_metric_swaps(self):
        run = source("My teammates developed the model.")
        plan = self.plan(run, confirmed_facts=[fact("I built the interface.")])
        context = plan["technical_context"]
        self.assertTrue(check_technical_accuracy(context, "My teammates developed the model. I built the interface.", used_fact_ids=["FACT-BACKEND"])["passed"])
        for candidate in ("My teammates developed the model. I trained the model.",
                          "My teammates developed the model. I independently developed the underlying model."):
            self.assertFalse(check_technical_accuracy(context, candidate, used_fact_ids=["FACT-BACKEND"])["passed"])
        metrics = extract_technical_context({"draft": "We tested the parser.", "evidence": {}}, confirmed_facts=[
            fact("We measured MRR 0.42 and NDCG 0.68.", fact_id="FACT-METRICS")])
        self.assertFalse(check_technical_accuracy(metrics, "We tested the parser. We measured MRR 0.68 and NDCG 0.42.", used_fact_ids=["FACT-METRICS"])["passed"])

    def test_full_revision_can_trace_confirmed_technical_facts(self):
        run = source()
        text = "I implemented a retrieval backend."
        plan = self.plan(run, confirmed_facts=[fact(text)])
        candidate = "I built an interface and implemented a retrieval backend."
        claim = next(claim for claim in plan["technical_context"]["claims"] if claim["source_type"] == "confirmed_fact")
        def trace_fact(stage, output, payload):
            if stage != "verify_revision":
                output["used_fact_ids"] = ["FACT-BACKEND"]
            else:
                output["claim_changes"].append({"original_quote": None, "final_quote": "implemented a retrieval backend",
                    "source_type": "confirmed_fact", "source_id": "FACT-BACKEND", "source_quote": text, "status": "supported"})
                output["technical_checks"].append({"claim_id": claim["id"], "status": "supported_change",
                    "original_quote": text, "final_quote": "implemented a retrieval backend", "source_type": "confirmed_fact",
                    "source_id": "FACT-BACKEND", "source_quote": text, "reason": "This is the precise applicant-confirmed contribution."})
            return output
        record = revise_document(run, plan, TechnicalEditor(candidate, trace_fact))
        self.assertEqual(record["status"], "ready", record["validation"])
        self.assertTrue(record["technical_accuracy"]["passed"])
        self.assertTrue(verify_revision(record))
        mutated = copy.deepcopy(plan)
        next(source for source in mutated["technical_context"]["sources"] if source["source_type"] == "confirmed_fact")["text"] = "I trained BERT."
        mutated["plan_sha256"] = fingerprint({key: value for key, value in mutated.items() if key != "plan_sha256"})
        with self.assertRaises(ValueError):
            validate_revision_plan(run, mutated)

    def test_technical_verifier_cannot_cite_an_unreported_confirmed_fact(self):
        plan = self.plan(confirmed_facts=[fact("I built the interface.")])
        context = plan["technical_context"]
        draft_claim = next(claim for claim in context["claims"] if claim["source_type"] == "draft")
        checks = [{"claim_id": draft_claim["id"], "status": "preserved", "original_quote": draft_claim["quote"],
                   "final_quote": "I built an interface.", "source_type": "confirmed_fact", "source_id": "FACT-BACKEND",
                   "source_quote": "I built the interface.", "reason": "Known author confirmation."}]
        report = check_technical_accuracy(context, "I built an interface.", checks, used_fact_ids=[])
        self.assertFalse(report["passed"])
        self.assertTrue(any(issue["kind"] == "unreported_confirmed_fact" for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
