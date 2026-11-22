import copy
import json
import unittest

from admissions_editor.generation import generate_draft
from admissions_editor.providers import ROLE_NAMES
from admissions_editor.review import apply_changes, fingerprint, run_review
from admissions_editor.workflow import (RuleWorkflowProvider, enrich_run, seal_run,
                                        retry_adjudication, validated_changes, verify_run)

DRAFT = "I built the the prototype."


class Reviewer:
    label = "openai:SECRET-MODEL-IDENTITY"

    def review(self, role, payload):
        finding = {"kind": "issue", "category": "language", "severity": "low", "paragraph_id": "P001",
                   "quote": "the the prototype", "rationale": "Remove the repeated article. openai:SECRET-MODEL-IDENTITY", "action": "revise",
                   "replacement": "the prototype", "evidence_ids": []}
        return {"role": role, "findings": [finding] if role == "language" else []}


class RecordingWorkflow(RuleWorkflowProvider):
    def __init__(self):
        self.calls = []

    def task(self, task, payload, schema):
        self.calls.append((task, copy.deepcopy(payload)))
        result = super().task(task, payload, schema)
        # A misbehaving adapter must not mutate the original frozen data.
        payload.get("snapshot", {})["draft"] = "MUTATED"
        return result


class WorkflowTest(unittest.TestCase):
    def run_review(self, workflow=None, draft=DRAFT):
        return enrich_run(run_review(Reviewer(), draft.encode(), "Describe a project.", "", {}),
                          workflow or RuleWorkflowProvider(), as_of="2026-11-18")

    def test_adjudicator_observes_original_first_then_anonymous_opinions(self):
        provider = RecordingWorkflow()
        run = self.run_review(provider)
        self.assertEqual(run["status"], "complete")
        self.assertEqual([c[0] for c in provider.calls], ["observe", "decide"])
        self.assertEqual(set(provider.calls[0][1]), {"snapshot", "ledger"})
        second = provider.calls[1][1]
        self.assertIn("independent_observations", second)
        self.assertIn("groups", second)
        self.assertNotIn("SECRET-MODEL-IDENTITY", json.dumps(second))
        self.assertNotIn("provider", json.dumps(second))
        self.assertEqual(run["snapshot"]["draft"], DRAFT)
        self.assertEqual(apply_changes(DRAFT, validated_changes(run)), "I built the prototype.")

    def test_observation_failure_prevents_second_stage_and_disables_approval(self):
        class Broken(RecordingWorkflow):
            def task(self, task, payload, schema):
                self.calls.append((task, payload))
                return {"observations": [{"paragraph_id": "P001", "quote": "invented", "assessment": "x", "evidence_ids": []}]}

        provider = Broken()
        run = self.run_review(provider)
        self.assertEqual([c[0] for c in provider.calls], ["observe"])
        self.assertEqual(run["status"], "partial")
        self.assertFalse(run["changes"])
        with self.assertRaises(ValueError):
            validated_changes(run)

    def test_missing_adjudication_item_fails_closed_but_retains_reviews(self):
        class Broken(RuleWorkflowProvider):
            def task(self, task, payload, schema):
                return {"decisions": []} if task == "decide" else super().task(task, payload, schema)

        run = self.run_review(Broken())
        self.assertEqual(run["status"], "partial")
        self.assertEqual(len(run["reviewers"]["language"]["findings"]), 1)
        self.assertEqual(len(run["adjudication"]["requests"]), 2)
        error = run["adjudication"]["validation_errors"][0]
        self.assertEqual(error["coverage"]["missing_group_ids"], ["F-001"])
        self.assertEqual(run["adjudication"]["unvalidated_decisions"], [])

    def test_adjudicator_veto_withholds_an_otherwise_eligible_change(self):
        class Veto(RuleWorkflowProvider):
            def task(self, task, payload, schema):
                result = super().task(task, payload, schema)
                if task == "decide":
                    result["decisions"][0]["decision"] = "preserve"
                return result

        run = self.run_review(Veto())
        self.assertFalse(run["changes"])
        self.assertFalse(validated_changes(run))

    def test_audit_tampering_and_modified_proposal_fail_revalidation(self):
        run = self.run_review()
        run["audit"][0]["data"]["input_sha256"] = "tampered"
        with self.assertRaises(ValueError):
            verify_run(run)
        run = self.run_review()
        run["changes"][0]["replacement"] = "I invented a publication."
        seal_run(run)  # Even recalculating a record checksum cannot bypass proposal rules.
        with self.assertRaises(ValueError):
            validated_changes(run)

    def test_input_evidence_and_preferences_versions_change_proposal_ids(self):
        ids = []
        for preference, evidence in (("", {}), ("Keep my voice", {}), ("", {"new.txt": "new evidence"})):
            run = enrich_run(run_review(Reviewer(), DRAFT.encode(), "Describe a project.", preference, evidence),
                             RuleWorkflowProvider(), as_of="2026-11-18")
            ids.append(run["changes"][0]["id"])
        self.assertEqual(len(set(ids)), 3)

    def test_reviewer_exceptions_are_isolated_and_do_not_log_sensitive_messages(self):
        class Broken(Reviewer):
            def review(self, role, payload):
                if role == "skeptical":
                    raise TypeError("PRIVATE-PAYLOAD-OR-SECRET")
                return super().review(role, payload)

        run = run_review(Broken(), DRAFT.encode(), "Describe a project.", "", {})
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["reviewers"]["language"]["status"], "complete")
        self.assertNotIn("PRIVATE-PAYLOAD-OR-SECRET", json.dumps(run))
        self.assertFalse(run["changes"])

    def test_generation_uses_confirmed_materials_marks_gaps_and_hides_writer_metadata(self):
        provider = RecordingWorkflow()
        draft, record = generate_draft(provider, {"materials": [
            {"id": "M1", "text": "I built the the prototype.", "confirmed": True},
            {"id": "M2", "text": "DO-NOT-USE-UNCONFIRMED", "confirmed": False}], "questions": ["Which program?"]}, "Describe a project.")
        self.assertNotIn("DO-NOT-USE-UNCONFIRMED", draft)
        self.assertNotIn("DO-NOT-USE-UNCONFIRMED", json.dumps(provider.calls))
        self.assertIn("[待补:", draft)
        self.assertEqual(record["paragraph_material_map"][0]["material_ids"], ["M1"])
        captured = []

        class Capture(Reviewer):
            def review(self, role, payload):
                captured.append(payload)
                return {"role": role, "findings": []}

        run_review(Capture(), draft.encode(), "Describe a project.", "", {})
        self.assertEqual(len(captured), 4)
        for payload in captured:
            self.assertEqual(set(payload), {"draft", "paragraphs", "requirements", "preferences", "evidence"})
            self.assertNotIn("paragraph_material_map", json.dumps(payload))

    def test_rule_adjudication_is_invariant_to_opinion_order(self):
        original = run_review(Reviewer(), DRAFT.encode(), "Describe a project.", "", {})
        forward = enrich_run(copy.deepcopy(original), RuleWorkflowProvider(), as_of="2026-11-18")
        reverse = enrich_run(copy.deepcopy(original), RuleWorkflowProvider(), as_of="2026-11-18", reverse=True)
        self.assertEqual(forward["adjudication"]["decisions"], reverse["adjudication"]["decisions"])

    def test_fact_disagreement_retains_both_views_and_one_cross_round_issue(self):
        class Divergent(Reviewer):
            def review(self, role, payload):
                finding = {"kind": "issue" if role == "admissions" else "question", "category": "evidence",
                           "severity": "high", "paragraph_id": "P001", "quote": DRAFT,
                           "rationale": "Conflicting synthetic reading; author verification needed.",
                           "action": "revise" if role == "admissions" else "verify", "replacement": None, "evidence_ids": []}
                return {"role": role, "findings": [finding] if role in {"admissions", "skeptical"} else []}

        run = enrich_run(run_review(Divergent(), DRAFT.encode(), "Describe a project.", "", {}),
                         RuleWorkflowProvider(), as_of="2026-11-18")
        self.assertEqual(len(run["groups"]), 2)
        self.assertEqual(len(run["issues"]), 1)
        self.assertEqual(len(run["issues"][0]["group_ids"]), 2)
        self.assertIn("fact_conclusion_requires_review", run["disagreements"][0]["types"])
        self.assertIn("opposite_actions", run["disagreements"][0]["types"])
        self.assertTrue(all(d["decision"] == "author_decision" for d in run["adjudication"]["decisions"]))

    def test_claim_id_in_evidence_references_reports_the_stage_and_field(self):
        class ClaimAsSource(RecordingWorkflow):
            def task(self, task, payload, schema):
                self.calls.append((task, payload))
                return {"observations": [{"paragraph_id": "P001", "quote": DRAFT,
                        "assessment": "Unverified synthetic claim.",
                        "evidence_ids": [payload["ledger"]["claims"][0]["id"]]}]}

        provider = ClaimAsSource()
        run = self.run_review(provider)
        self.assertEqual(run["status"], "partial")
        error = run["adjudication"]["validation_errors"][0]
        self.assertEqual(error["code"], "unknown_evidence_id")
        self.assertEqual(error["stage"], "observe")
        self.assertEqual(error["path"], "observations[0].evidence_ids")
        self.assertEqual([c[0] for c in provider.calls], ["observe"])
        self.assertFalse(run["changes"])

    def test_retry_preserves_cached_reviews_binds_new_ids_and_keeps_original_unchanged(self):
        class BadObservation(RuleWorkflowProvider):
            def task(self, task, payload, schema):
                return {"observations": [{"paragraph_id": "P001", "quote": DRAFT,
                         "assessment": "Synthetic observation.", "evidence_ids": ["AUTO-not-a-source"]}]}

        old = self.run_review(BadObservation())
        unchanged = copy.deepcopy(old)
        provider = RecordingWorkflow()
        retried = retry_adjudication(old, provider, "synthetic-original-record-hash")
        self.assertEqual([c[0] for c in provider.calls], ["observe", "decide"])
        self.assertEqual(old, unchanged)
        self.assertEqual(retried["status"], "complete")
        self.assertNotEqual(retried["run_id"], old["run_id"])
        self.assertNotEqual(retried["input_sha256"], old["input_sha256"])
        self.assertEqual(retried["round"]["number"], old["round"]["number"])
        self.assertEqual(retried["ledger"], old["ledger"])
        for role in ROLE_NAMES:
            self.assertEqual(retried["reviewers"][role]["output"], old["reviewers"][role]["output"])
            self.assertEqual(retried["reviewers"][role]["reused_from_run_sha256"], "synthetic-original-record-hash")
        self.assertEqual(apply_changes(DRAFT, validated_changes(retried)), "I built the prototype.")
        verify_run(retried)

    def test_retry_rejects_missing_or_tampered_reviews_and_does_not_leak_provider_errors(self):
        class BadWorkflow(RecordingWorkflow):
            def task(self, task, payload, schema):
                raise ValueError("PRIVATE-PROVIDER-PAYLOAD-SECRET")

        original = self.run_review(BadWorkflow())
        self.assertNotIn("PRIVATE-PROVIDER-PAYLOAD-SECRET", json.dumps(original))
        original["reviewers"]["language"]["findings"][0]["replacement"] = "invented achievement"
        seal_run(original)
        provider = RecordingWorkflow()
        with self.assertRaises(ValueError):
            retry_adjudication(original, provider, "synthetic-source-hash")
        self.assertEqual(provider.calls, [])

    def failed_decision_run(self, empty_observations=False):
        class MissingDecision(RecordingWorkflow):
            def task(self, task, payload, schema):
                if task == "decide" or (task == "observe" and empty_observations):
                    self.calls.append((task, copy.deepcopy(payload)))
                    return {"decisions": []} if task == "decide" else {"observations": []}
                return super().task(task, payload, schema)

        return self.run_review(MissingDecision())

    def test_decision_retry_reuses_valid_independent_observations_with_one_task(self):
        old = self.failed_decision_run()
        original = copy.deepcopy(old)
        provider = RecordingWorkflow()
        retried = retry_adjudication(old, provider, "synthetic-partial-record")
        self.assertEqual([c[0] for c in provider.calls], ["decide"])
        self.assertEqual(old, original)
        self.assertEqual(retried["status"], "complete")
        self.assertTrue(retried["recovery"]["reused_observation"])
        self.assertTrue(retried["adjudication"]["requests"][0]["reused"])
        self.assertEqual(retried["adjudication"]["independent_observations"], old["adjudication"]["independent_observations"])
        self.assertEqual(apply_changes(DRAFT, validated_changes(retried)), "I built the prototype.")

    def test_legacy_empty_observation_is_recomputed_after_wire_schema_fix(self):
        old = self.failed_decision_run(empty_observations=True)
        old["adjudication"]["requests"][0]["prompt_version"] = "observe-2"
        seal_run(old)
        provider = RecordingWorkflow()
        retried = retry_adjudication(old, provider, "synthetic-legacy-record")
        self.assertEqual([c[0] for c in provider.calls], ["observe", "decide"])
        self.assertFalse(retried["recovery"]["reused_observation"])
        self.assertTrue(retried["adjudication"]["independent_observations"]["observations"])

    def test_observation_cache_is_invalidated_by_model_or_response_hash_changes(self):
        for change in ("provider", "response"):
            with self.subTest(change=change):
                old = self.failed_decision_run()
                provider = RecordingWorkflow()
                if change == "provider":
                    provider.label = "rule:different-adjudicator"
                else:
                    old["adjudication"]["independent_observations"] = {"observations": []}
                    seal_run(old)
                retried = retry_adjudication(old, provider, "synthetic-record")
                self.assertEqual([c[0] for c in provider.calls], ["observe", "decide"])
                self.assertFalse(retried["recovery"]["reused_observation"])

    def test_duplicate_decisions_are_retained_as_unvalidated_and_block_approval(self):
        class Duplicated(RecordingWorkflow):
            def task(self, task, payload, schema):
                result = super().task(task, payload, schema)
                if task == "decide":
                    result["decisions"] *= 2
                return result

        run = self.run_review(Duplicated())
        self.assertEqual(run["status"], "partial")
        coverage = run["adjudication"]["validation_errors"][0]["coverage"]
        self.assertEqual(coverage["expected_count"], 1)
        self.assertEqual(coverage["returned_count"], 2)
        self.assertEqual(coverage["duplicate_group_ids"], ["F-001"])
        self.assertEqual(len(run["adjudication"]["unvalidated_decisions"]), 2)
        self.assertFalse(run["changes"])
        with self.assertRaises(ValueError):
            validated_changes(run)


if __name__ == "__main__":
    unittest.main()
