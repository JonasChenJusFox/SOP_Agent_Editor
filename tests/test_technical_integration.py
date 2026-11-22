"""Offline full-workflow checks for technical meaning and source-bound decisions."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from admissions_editor.cli import main, publish_directory, review_artifacts
from admissions_editor.review import fingerprint, paragraph_spans, run_review, sha256
from admissions_editor.revision import (ALL_STAGES, finalize_revision, json_bytes, prepare_revision_plan,
                                       revise_document, revision_artifacts, validate_revision_plan, verify_revision)
from admissions_editor.revision_schema import CHECK_NAMES
from admissions_editor.workflow import RuleWorkflowProvider, enrich_run


DRAFT = ("My team built Atlas, a retrieval interface using embeddings and FAISS.\n\n"
         "I implemented the interface. My teammates developed the ranking model. I did not fine-tune the model.\n\n"
         "We evaluated recall on the test set.")
EVIDENCE = {"project-notes.txt": "The applicant implemented the interface using FAISS embeddings. "
            "The applicant did not train or fine-tune the model. Teammates developed the ranking model. "
            "The team evaluated recall on the test set."}
GLOSSARY = [{"term": "Atlas", "definition": "The project's retrieval interface.", "aliases": ["Atlas interface"]}]


class ClearReviewer:
    label = "fixture:technical-clear-review"

    def review(self, role, payload):
        return {"role": role, "findings": []}


def source():
    run = run_review(ClearReviewer(), DRAFT.encode(), "Describe your actual technical contribution. Maximum 500 English words.",
                     "Keep technical meaning and distinguish individual work from team results.", EVIDENCE)
    return enrich_run(run, RuleWorkflowProvider())


class TechnicalEditor:
    label = "fixture:technical-editor"

    def __init__(self, essay=DRAFT, change=None):
        self.essay, self.change, self.calls = essay, change, []

    def task(self, stage, payload, schema):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage != "verify_revision":
            output = {"essay": self.essay, "addressed_finding_ids": [], "used_fact_ids": [],
                      "unresolved_questions": [], "technical_flags": []}
        else:
            original = payload["original"]
            candidate = payload["current_essay"]
            final_paragraphs = list(paragraph_spans(candidate).values())
            context = payload["technical_context"]
            claims = {claim["id"]: claim for claim in context["claims"]}
            technical_checks = []
            for claim_id in context["check_required_claim_ids"]:
                claim = claims[claim_id]
                final_quote = claim["quote"] if claim["quote"] in candidate else final_paragraphs[int(claim["source_id"][1:]) - 1][1]
                technical_checks.append({"claim_id": claim_id,
                    "status": "preserved" if claim["quote"] in candidate else "supported_change",
                    "original_quote": claim["quote"], "final_quote": final_quote,
                    "source_type": claim["source_type"], "source_id": claim["source_id"],
                    "source_quote": claim["quote"], "reason": "Synthetic audit preserves the method, component and actor relationships."})
            output = {"checks": [{"check": name, "passed": True, "rationale": "Synthetic general check."} for name in CHECK_NAMES],
                      "finding_checks": [], "claim_changes": [{"original_quote": paragraph["text"],
                        "final_quote": final_paragraphs[index][1], "source_type": "original", "source_id": paragraph["id"],
                        "source_quote": paragraph["text"], "status": "supported"} for index, paragraph in enumerate(original["paragraphs"])],
                      "unresolved_questions": [], "technical_checks": technical_checks}
        return self.change(stage, output, payload) if self.change else output


class TechnicalRevisionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.run = source()
        self.run_hash = sha256(json_bytes(self.run))
        self.plan = prepare_revision_plan(self.run, self.run_hash, glossary=GLOSSARY)

    def assert_blocked(self, record):
        self.assertIn(record["status"], {"blocked", "failed"})
        self.assertFalse(record["technical_accuracy"]["passed"])
        self.assertTrue(record["technical_accuracy"]["issues"])
        self.assertNotIn("final-essay.txt", revision_artifacts(record))
        with self.assertRaises(ValueError):
            finalize_revision(record, record["candidate_sha256"], record["plan"]["plan_sha256"])
        self.assertTrue(verify_revision(record))

    def test_glossary_and_frozen_technical_context_reach_all_three_writers_before_read_only_check(self):
        candidate = DRAFT.replace("My team built", "Our team built")
        provider = TechnicalEditor(essay=candidate)
        before_run, before_plan = copy.deepcopy(self.run), copy.deepcopy(self.plan)
        record = revise_document(self.run, self.plan, provider)
        self.assertEqual(record["status"], "ready", record["validation"])
        self.assertEqual([stage for stage, _ in provider.calls], list(ALL_STAGES))
        for stage, payload in provider.calls:
            self.assertEqual(payload["technical_context"], self.plan["technical_context"], stage)
            self.assertEqual(payload["technical_context"]["glossary"], GLOSSARY)
            self.assertEqual(payload["original"], self.run["snapshot"])
        final_payload = provider.calls[-1][1]
        self.assertEqual(final_payload["versions"]["humanize"], candidate)
        baseline = final_payload["mechanical_checks"]["technical_accuracy"]
        self.assertTrue(baseline["passed"])
        self.assertTrue(baseline["model_verification_pending"])
        self.assertNotIn("Humanizer 后的只读技术准确性核验尚未完成。", final_payload["mechanical_checks"]["blockers"])
        self.assertEqual(record["candidate"], candidate)
        self.assertEqual(record["technical_accuracy"]["checks"], record["stages"][-1]["output"]["technical_checks"])
        self.assertEqual(self.run, before_run)
        self.assertEqual(self.plan, before_plan)
        artifacts = revision_artifacts(record)
        self.assertEqual(json.loads(artifacts["technical-context.json"]), self.plan["technical_context"])
        self.assertEqual(json.loads(artifacts["technical-accuracy.json"]), record["technical_accuracy"])
        self.assertTrue(verify_revision(record))

    def test_glossary_or_extracted_source_changes_cannot_hide_behind_a_new_outer_checksum(self):
        for mutate in (
            lambda plan: plan["glossary"][0].update(definition="The applicant trained the underlying model."),
            lambda plan: plan["technical_context"]["claims"][0].update(quote="I trained the underlying model."),
            lambda plan: plan["technical_context"]["sources"][0].update(text="Forged project history."),
            lambda plan: plan["technical_context"].update(check_required_claim_ids=[]),
        ):
            changed = copy.deepcopy(self.plan)
            mutate(changed)
            changed["plan_sha256"] = fingerprint({key: value for key, value in changed.items() if key != "plan_sha256"})
            with self.assertRaises(ValueError):
                validate_revision_plan(self.run, changed)

    def test_meaning_drift_is_rejected_before_the_bad_proposal_reaches_later_stages(self):
        replacements = (("retrieval interface", "ranking interface"),
                        ("using embeddings and FAISS", "using fine-tuning and FAISS"),
                        ("I implemented the interface.", "I developed the underlying model."))
        for bad_stage in ("sop_revise", "polish", "humanize"):
            for original, replacement in replacements:
                def risky_edit(stage, output, payload):
                    if stage == bad_stage:
                        output["essay"] = output["essay"].replace(original, replacement)
                    return output
                with self.subTest(stage=bad_stage, replacement=replacement):
                    provider = TechnicalEditor(change=risky_edit)
                    record = revise_document(self.run, self.plan, provider)
                    self.assert_blocked(record)
                    self.assertEqual(record["candidate"], DRAFT)
                    self.assertEqual(record["rejected_stage"]["stage"], bad_stage)
                    self.assertIn(replacement, record["rejected_stage"]["output"]["essay"])
                    self.assertEqual(provider.calls[-1][0], bad_stage)
                    self.assertNotIn("verify_revision", [stage for stage, _ in provider.calls])
                    issues = record["technical_accuracy"]["issues"]
                    self.assertTrue(any(issue["source_id"] and issue["source_quote"] for issue in issues))

    def test_rejected_proposal_cannot_be_forged_even_with_recomputed_record_checksum(self):
        def risky(stage, output, payload):
            if stage == "humanize":
                output["essay"] = output["essay"].replace("retrieval interface", "ranking interface")
            return output
        record = revise_document(self.run, self.plan, TechnicalEditor(change=risky))
        self.assert_blocked(record)
        changed = copy.deepcopy(record)
        changed["rejected_stage"]["output"]["essay"] = DRAFT
        changed["rejected_stage"]["response_sha256"] = fingerprint(changed["rejected_stage"]["output"])
        changed["record_sha256"] = fingerprint({key: value for key, value in changed.items() if key != "record_sha256"})
        with self.assertRaises(ValueError):
            verify_revision(changed)

    def test_earlier_technical_flag_keeps_original_wording_when_a_later_writer_omits_the_flag(self):
        claim = next(claim for claim in self.plan["technical_context"]["claims"]
                     if claim["source_type"] == "draft" and "My team built" in claim["quote"])
        def flagged(stage, output, payload):
            if stage == "sop_revise":
                output["technical_flags"] = [{"claim_id": claim["id"], "reason": "Confirm project terminology before rephrasing."}]
            if stage == "polish":
                output["essay"] = output["essay"].replace("My team built", "Our team built")
            return output
        record = revise_document(self.run, self.plan, TechnicalEditor(change=flagged))
        self.assert_blocked(record)
        self.assertEqual(record["candidate"], DRAFT)
        self.assertEqual(record["rejected_stage"]["stage"], "polish")
        self.assertTrue(any(issue["kind"] == "flagged_wording_changed" for issue in record["technical_accuracy"]["issues"]))

    def test_retained_flagged_wording_can_be_resolved_by_read_only_supported_source_check(self):
        claim = next(claim for claim in self.plan["technical_context"]["claims"] if claim["source_type"] == "draft")
        def flagged(stage, output, payload):
            if stage == "sop_revise":
                output["technical_flags"] = [{"claim_id": claim["id"], "reason": "Keep this wording pending technical verification."}]
            return output
        record = revise_document(self.run, self.plan, TechnicalEditor(change=flagged))
        self.assertEqual(record["status"], "ready", record["validation"])
        self.assertEqual(record["candidate"], DRAFT)
        self.assertTrue(record["technical_accuracy"]["writing_flags"])
        self.assertTrue(record["technical_accuracy"]["complete"])

    def test_technical_failure_cannot_be_overruled_by_positive_general_checks(self):
        mutations = {
            "altered": lambda checks: checks[0].update(status="altered", reason="The technical meaning changed."),
            "unsupported": lambda checks: checks[0].update(status="unsupported", reason="Source does not support this claim."),
            "ambiguous": lambda checks: checks[0].update(status="ambiguous", reason="Confirm which component the applicant built."),
            "omitted": lambda checks: checks.pop(),
            "forged_source": lambda checks: checks[0].update(source_id="invented-source.txt"),
            "forged_quote": lambda checks: checks[0].update(source_quote="The applicant trained the model."),
            "not_used_for_draft": lambda checks: checks[0].update(status="not_used", final_quote=None),
        }
        for name, mutate in mutations.items():
            def invalid(stage, output, payload):
                if stage == "verify_revision":
                    mutate(output["technical_checks"])
                return output
            with self.subTest(case=name):
                record = revise_document(self.run, self.plan, TechnicalEditor(change=invalid))
                self.assert_blocked(record)
                self.assertTrue(all(check["passed"] for check in record["stages"][-1]["output"]["checks"]))
                self.assertEqual(record["candidate"], DRAFT)

    def test_glossary_definition_is_not_evidence_for_model_training_or_an_unknown_library(self):
        glossary = GLOSSARY + [{"term": "PhantomTrainer", "definition": "A project training system the applicant developed.", "aliases": []}]
        plan = prepare_revision_plan(self.run, self.run_hash, glossary=glossary)
        self.assertTrue(all("PhantomTrainer" not in source["text"] for source in plan["technical_context"]["sources"]))
        for addition in (" I developed PhantomTrainer.", " I trained the underlying model."):
            def invent(stage, output, payload):
                if stage == "humanize":
                    output["essay"] += addition
                return output
            with self.subTest(addition=addition):
                record = revise_document(self.run, plan, TechnicalEditor(change=invent))
                self.assert_blocked(record)
                self.assertEqual(record["candidate"], DRAFT)

    def test_cli_accepts_optional_glossary_and_exports_the_bound_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = root / "review"
            publish_directory(review, review_artifacts(self.run))
            draft = root / "draft.txt"
            draft.write_text(DRAFT)
            glossary = root / "glossary.json"
            glossary.write_text(json.dumps(GLOSSARY))
            output = root / "plan"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["revision-plan", "--run", str(review), "--draft", str(draft),
                             "--glossary", str(glossary), "--out", str(output)])
            self.assertEqual(code, 0)
            plan = json.loads((output / "plan.json").read_text())
            self.assertEqual(plan["glossary"], GLOSSARY)
            self.assertEqual(json.loads((output / "technical-context.json").read_text()), plan["technical_context"])
            self.assertTrue(validate_revision_plan(self.run, plan))


if __name__ == "__main__":
    unittest.main()
