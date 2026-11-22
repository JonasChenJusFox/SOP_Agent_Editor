import copy
import json
import unittest
from pathlib import Path

from admissions_editor.providers import FixtureProvider
from admissions_editor.review import fingerprint, read_evidence, run_review, sha256
from admissions_editor.workflow import RuleWorkflowProvider, enrich_run, seal_run
from admissions_editor.revision import (ALL_STAGES, _numbers, finalize_revision, json_bytes,
                                       prepare_revision_plan, revise_document, revision_artifacts,
                                       validate_revision_plan, verify_revision)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FIXTURES = EXAMPLES / "revision"


def make_source():
    data = json.loads((FIXTURES / "demo.json").read_text())
    source = data["input"]
    run = run_review(FixtureProvider(EXAMPLES / "fixtures"), source["draft"].encode(), source["requirements"],
                     source["preferences"], {f["id"]: f["text"] for f in source["evidence"]})
    run = enrich_run(run, RuleWorkflowProvider(), manifest=source["claims"], source_metadata=source["sources"], as_of=source["as_of"])
    return run, data["plan_input"]


class Editor(FixtureProvider):
    def __init__(self, change=None):
        super().__init__(FIXTURES / "fixtures")
        self.calls, self.change = [], change

    def task(self, task, payload, schema):
        self.calls.append((task, copy.deepcopy(payload)))
        result = super().task(task, payload, schema)
        return self.change(task, result) if self.change else result


class RevisionTest(unittest.TestCase):
    def setUp(self):
        self.run, self.options = make_source()
        self.run_sha = sha256(json_bytes(self.run))

    def plan(self, **kwargs):
        return prepare_revision_plan(self.run, self.run_sha, **{**copy.deepcopy(self.options), **kwargs})

    def record(self, provider=None, **kwargs):
        return revise_document(self.run, self.plan(**kwargs), provider or Editor())

    def test_full_fixture_reorders_essay_then_polishes_humanizes_and_read_only_verifies(self):
        provider = Editor()
        record = self.record(provider)
        self.assertEqual(record["status"], "ready", record["validation"])
        self.assertEqual([stage for stage, _ in provider.calls], list(ALL_STAGES))
        self.assertNotEqual(record["candidate"], self.run["snapshot"]["draft"])
        self.assertTrue(record["candidate"].startswith("I want to understand"))
        self.assertNotIn("eliminated delays", record["candidate"])
        self.assertIn("20 saved messages", record["candidate"])
        self.assertIn("Northbridge", record["candidate"])
        verify_revision(record)
        artifacts = revision_artifacts(record)
        for name in ("candidate.txt", "full.diff", "trace.json", "plan.json", "validation.json", "final-essay.txt"):
            self.assertIn(name, artifacts)
        self.assertTrue(all(row["status"] == "addressed" for row in record["trace"]))
        self.assertTrue(all(row["final_quote"] for row in record["trace"]))
        text, receipt = finalize_revision(record, record["candidate_sha256"], record["plan"]["plan_sha256"])
        self.assertEqual(text, (FIXTURES / "expected-final.txt").read_text())
        self.assertEqual(receipt["final_sha256"], sha256(text.encode()))
        self.assertEqual(receipt["author_action"], "accept_full_revision")

    def test_plan_deterministic_and_stale_plan_or_original_rejected(self):
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        changed = copy.deepcopy(self.run)
        changed["snapshot"]["draft"] += " New source."
        with self.assertRaises(ValueError):
            validate_revision_plan(changed, plan)
        plan["draft_sha256"] = "f" * 64
        plan["plan_sha256"] = fingerprint({k: v for k, v in plan.items() if k != "plan_sha256"})
        with self.assertRaises(ValueError):
            validate_revision_plan(self.run, plan)

    def test_missing_duplicate_unknown_decisions_fail_before_model(self):
        for decisions in (self.options["decisions"][:-1], self.options["decisions"] + self.options["decisions"][:1],
                          [{"finding_id": "unknown", "decision": "accept", "reason": "x"}]):
            with self.subTest(decisions=decisions):
                with self.assertRaises(ValueError):
                    self.plan(decisions=decisions)

    def test_default_template_explicitly_defers_and_lists_unresolved_questions(self):
        plan = prepare_revision_plan(self.run, self.run_sha)
        self.assertTrue(all(row["decision"] == "defer" for row in plan["findings"]))
        self.assertTrue(plan["unresolved_questions"])
        self.assertEqual(plan["confirmed_facts"], [])

    def test_missing_applicant_answer_cannot_be_overruled_by_positive_verifier(self):
        record = self.record(answers={})
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(any("请作者回答" in b for b in record["validation"]["blockers"]))
        self.assertNotIn("final-essay.txt", revision_artifacts(record))
        with self.assertRaises(ValueError):
            finalize_revision(record, record["candidate_sha256"])

    def test_incomplete_review_and_conflicting_author_decisions_do_not_call_editor(self):
        broken = copy.deepcopy(self.run)
        broken["status"] = "partial"
        broken["reviewers"]["skeptical"]["status"] = "failed"
        seal_run(broken)
        plan = prepare_revision_plan(broken, sha256(json_bytes(broken)), **self.options)
        provider = Editor()
        record = revise_document(broken, plan, provider)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(provider.calls, [])
        decisions = copy.deepcopy(self.options["decisions"])
        next(row for row in decisions if row["finding_id"] == "admissions-01")["decision"] = "reject"
        provider = Editor()
        record = self.record(provider, decisions=decisions)
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(any("重叠" in b for b in record["validation"]["blockers"]))
        self.assertEqual(provider.calls, [])

    def test_rejected_grammar_finding_is_not_silently_applied(self):
        decisions = copy.deepcopy(self.options["decisions"])
        next(row for row in decisions if row["finding_id"] == "language-01")["decision"] = "reject"
        record = self.record(decisions=decisions)
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(any("拒绝/保留" in b for b in record["validation"]["blockers"]))
        self.assertNotIn("final-essay.txt", revision_artifacts(record))

    def test_unconfirmed_facts_and_writing_sample_never_become_factual_authority(self):
        provider = Editor()
        facts = self.options["confirmed_facts"] + [{"id": "UNCONFIRMED", "text": "DO-NOT-SEND-PRIVATE-FACT", "confirmed": False}]
        record = self.record(provider, confirmed_facts=facts, writing_sample="My sample discusses 999 other topics.")
        self.assertEqual(record["status"], "blocked")
        self.assertNotIn("DO-NOT-SEND-PRIVATE-FACT", json.dumps(provider.calls))
        self.assertTrue(all("999 other topics" in call[1]["voice_reference"] for call in provider.calls))

    def test_new_number_and_percentage_unit_drift_block_even_if_verifier_passes(self):
        for replacement in ("999 saved messages", "20% saved messages", "20 years"):
            def change(stage, result):
                if stage == "humanize":
                    result["essay"] = result["essay"].replace("20 saved messages", replacement)
                return result
            with self.subTest(replacement=replacement):
                record = self.record(Editor(change))
                self.assertEqual(record["status"], "blocked")
                self.assertTrue(any("新增未确认数字" in b for b in record["validation"]["blockers"]))
        self.assertEqual(_numbers("5 seconds and 1.5 million"), _numbers("5 s and 1,500,000"))
        self.assertNotEqual(_numbers("40%"), _numbers("40 percentage points"))

    def test_local_edit_cannot_authorize_deleting_other_facts_in_its_paragraph(self):
        data = json.loads((FIXTURES / "demo.json").read_text())["input"]
        extra = " We logged 12 diagnostic events."
        data["draft"] = data["draft"].replace("Our system eliminated delays for everyone on campus.",
                                            "Our system eliminated delays for everyone on campus." + extra)
        run = run_review(FixtureProvider(EXAMPLES / "fixtures"), data["draft"].encode(), data["requirements"],
                         data["preferences"], {f["id"]: f["text"] for f in data["evidence"]})
        run = enrich_run(run, RuleWorkflowProvider(), manifest=data["claims"], source_metadata=data["sources"], as_of=data["as_of"])
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), **self.options)
        paragraph = run["snapshot"]["paragraphs"][1]["text"]

        def overbroad_deletion(stage, result):
            if stage == "verify_revision":
                for claim in result["claim_changes"]:
                    if claim["original_quote"] and "I built the the" in claim["original_quote"]:
                        claim["original_quote"] = paragraph
                result["claim_changes"][-1]["original_quote"] = paragraph
            return result

        record = revise_document(run, plan, Editor(overbroad_deletion))
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(any("删除的原稿数字 12" in b for b in record["validation"]["blockers"]))
        self.assertNotIn("final-essay.txt", revision_artifacts(record))

    def test_new_institution_or_contribution_expansion_blocks_final(self):
        for extra in (" I independently led the entire project.", " I studied at Atlantis University."):
            def change(stage, result):
                if stage == "humanize":
                    result["essay"] += extra
                return result
            record = self.record(Editor(change))
            self.assertEqual(record["status"], "blocked")
            self.assertTrue(any("贡献" in b or "人物/机构" in b for b in record["validation"]["blockers"]))

    def test_original_word_limit_cannot_be_relaxed_by_plan_override(self):
        self.assertEqual(self.plan(word_limit=1000)["word_limit"], 500)
        record = self.record(word_limit=50)
        self.assertEqual(record["status"], "blocked")
        self.assertTrue(any("字数上限" in b for b in record["validation"]["blockers"]))

    def test_provider_failure_retains_latest_candidate_and_redacts_error(self):
        class Broken(Editor):
            def task(self, task, payload, schema):
                if task == "polish":
                    raise RuntimeError("DO-NOT-LOG-SECRET-OR-MANUSCRIPT")
                return super().task(task, payload, schema)
        record = self.record(Broken())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["stages"][-1]["stage"], "sop_revise")
        self.assertIn("campus shuttle tracker", record["candidate"])
        self.assertNotIn("DO-NOT-LOG", json.dumps(record))
        self.assertNotIn("final-essay.txt", revision_artifacts(record))

    def test_verifier_failure_unknown_citation_or_omitted_trace_cannot_make_final(self):
        def false_check(stage, result):
            if stage == "verify_revision":
                result["checks"][0]["passed"] = False
            return result
        def fake_citation(stage, result):
            if stage == "verify_revision":
                result["claim_changes"][0]["source_id"] = "unknown"
            return result
        def no_trace(stage, result):
            if stage == "verify_revision":
                result["claim_changes"] = []
            return result
        for change in (false_check, fake_citation, no_trace):
            with self.subTest(change=change.__name__):
                record = self.record(Editor(change))
                self.assertEqual(record["status"], "blocked")
                with self.assertRaises(ValueError):
                    finalize_revision(record, record["candidate_sha256"])

    def test_verifier_status_must_match_the_accepted_finding_action(self):
        # A positive summary cannot turn an accepted revision/verification issue
        # into a preserve action, or silently rewrite an accepted preserve action.
        for finding_id, invalid_status in (("language-01", "preserved"),
                                           ("skeptical-01", "preserved"),
                                           ("structure-01", "addressed")):
            def wrong_status(stage, result):
                if stage == "verify_revision":
                    next(row for row in result["finding_checks"] if row["finding_id"] == finding_id)["status"] = invalid_status
                return result
            with self.subTest(finding_id=finding_id, status=invalid_status):
                record = self.record(Editor(wrong_status))
                self.assertEqual(record["status"], "blocked")
                self.assertTrue(any("不符合作者决定" in item for item in record["validation"]["blockers"]))
                self.assertNotIn("final-essay.txt", revision_artifacts(record))
                with self.assertRaises(ValueError):
                    finalize_revision(record, record["candidate_sha256"])

    def test_recomputed_outer_checksum_cannot_hide_changed_candidate_trace_or_validation(self):
        record = self.record()
        for field in ("candidate", "trace", "validation"):
            changed = copy.deepcopy(record)
            if field == "candidate": changed[field] += " Invented."
            elif field == "trace": changed[field] = []
            else: changed[field]["blockers"] = ["tampered"]
            changed["record_sha256"] = fingerprint({k: v for k, v in changed.items() if k != "record_sha256"})
            with self.assertRaises(ValueError):
                verify_revision(changed)
        with self.assertRaises(ValueError):
            finalize_revision(record, "stale-candidate")

    def test_provider_payload_mutations_do_not_change_review_or_plan(self):
        before = copy.deepcopy(self.run)
        class Mutates(Editor):
            def task(self, task, payload, schema):
                result = super().task(task, payload, schema)
                payload["original"]["draft"] = "MUTATED"
                payload["revision_plan"]["findings"].clear()
                return result
        record = self.record(Mutates())
        self.assertEqual(record["status"], "ready")
        self.assertEqual(self.run, before)

    def test_cloud_scheduling_base_exception_escapes_without_creating_failure_record(self):
        class Pending(BaseException): pass
        class CloudHost(Editor):
            def task(self, task, payload, schema): raise Pending()
        with self.assertRaises(Pending):
            self.record(CloudHost())


if __name__ == "__main__":
    unittest.main()
