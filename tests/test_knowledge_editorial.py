"""Synthetic repository provenance transport and editorial boundary checks."""

import copy
import json
import unittest

from admissions_editor.editorial_skills import load_editorial_skill
from admissions_editor.review import (fingerprint, knowledge_evidence, render_report, run_review,
                                      sha256, validate_knowledge_context)
from admissions_editor.revision import (ALL_STAGES, finalize_revision, json_bytes, prepare_revision_plan,
                                        revise_document, revision_artifacts, validate_revision_plan, verify_revision)
from admissions_editor.revision_schema import CHECK_NAMES
from admissions_editor.workflow import RuleWorkflowProvider, enrich_run, seal_run, verify_run

DRAFT = "I built a React interface for retrieval. My teammates trained the ranking model."
NOTES = {"author-notes.txt": "The applicant built the interface; teammates trained the model."}
CODE = "export function SearchInterface() {\n  return retrievalClient.search();\n}\n"


def knowledge(snapshot="a", passage="1", missing=False):
    source = {"name": "nearbite", "path": "/synthetic/private/knowledge/repos/nearbite", "status": "missing" if missing else "ready",
              "commit_sha": None if missing else "b" * 40, "snapshot_hash": None if missing else snapshot * 64,
              "file_count": 0 if missing else 1, "passage_count": 0 if missing else 1}
    passage_id = "KB-" + passage * 20
    passages = [] if missing else [{"id": passage_id, "evidence_id": passage_id, "repository": "nearbite",
        "commit_sha": "b" * 40, "snapshot_hash": snapshot * 64, "path": "src/search.js", "file_path": "src/search.js",
        "line_start": 1, "line_end": 3, "excerpt": CODE, "file_sha256": sha256(CODE.encode()),
        "kind": "source", "relevance_score": 1.5, "claim_ids": ["CLAIM-SEARCH"]}]
    context = {"schema_version": "knowledge-context-1", "status": "missing" if missing else "ready",
        "sources": [source], "claims": [{"id": "CLAIM-SEARCH", "claim_id": "CLAIM-SEARCH", "text": DRAFT,
                                          "passage_ids": [] if missing else [passage_id]}],
        "passages": passages, "diagnostics": ["Default source nearbite is missing."] if missing else []}
    context["snapshot_sha256"] = fingerprint(context)
    return context


class Reviewer:
    label = "fixture:knowledge-review"

    def __init__(self):
        self.calls = []

    def review(self, role, payload):
        self.calls.append((role, copy.deepcopy(payload)))
        return {"role": role, "findings": []}


def reviewed(context):
    provider = Reviewer()
    run = run_review(provider, DRAFT.encode(), "Describe your own project work.", "Preserve technical meaning.", NOTES,
                     knowledge_context=context)
    return enrich_run(run, RuleWorkflowProvider()), provider


class Editor:
    label = "fixture:knowledge-revision"

    def __init__(self):
        self.calls = []

    def task(self, stage, payload, schema):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage != "verify_revision":
            return {"essay": DRAFT, "addressed_finding_ids": [], "used_fact_ids": [], "unresolved_questions": [], "technical_flags": []}
        paragraphs = payload["original"]["paragraphs"]
        context = payload["technical_context"]
        claims = {claim["id"]: claim for claim in context["claims"]}
        return {"checks": [{"check": name, "passed": True, "rationale": "Synthetic unchanged-text check."} for name in CHECK_NAMES],
                "finding_checks": [], "unresolved_questions": [],
                "claim_changes": [{"original_quote": p["text"], "final_quote": p["text"], "source_type": "original",
                                    "source_id": p["id"], "source_quote": p["text"], "status": "supported"} for p in paragraphs],
                "technical_checks": [{"claim_id": claim_id, "status": "preserved", "original_quote": claims[claim_id]["quote"],
                    "final_quote": claims[claim_id]["quote"], "source_type": "draft", "source_id": claims[claim_id]["source_id"],
                    "source_quote": claims[claim_id]["quote"], "reason": "Synthetic preservation check; code does not independently establish authorship."}
                    for claim_id in context["check_required_claim_ids"]]}


class KnowledgeEditorialTest(unittest.TestCase):
    def test_each_reviewer_receives_the_same_frozen_claim_specific_context(self):
        context = knowledge()
        before = copy.deepcopy(context)
        run, provider = reviewed(context)
        self.assertEqual(len(provider.calls), 4)
        snapshots = [payload for _, payload in provider.calls]
        self.assertTrue(all(payload == snapshots[0] for payload in snapshots))
        self.assertEqual(snapshots[0]["knowledge_context"], context)
        self.assertEqual(snapshots[0]["evidence"][context["passages"][0]["id"]], CODE)
        self.assertEqual(context, before)
        self.assertNotIn("writer", json.dumps(snapshots))
        self.assertTrue(verify_run(run) is None)

    def test_editorial_report_shows_claim_locations_versions_and_coverage_limits(self):
        run, _ = reviewed(knowledge())
        report = render_report(run)
        for value in ("Repository knowledge", "CLAIM-SEARCH", "nearbite/src/search.js:L1-L3", "a" * 64,
                      "b" * 40, "SearchInterface", "not proof of authorship"):
            self.assertIn(value, report)
        missing_run, _ = reviewed(knowledge(missing=True))
        report = render_report(missing_run)
        self.assertIn("Default source nearbite is missing.", report)
        self.assertIn("No relevant repository passage retrieved", report)
        self.assertEqual(missing_run["status"], "complete")

    def test_malformed_or_stale_passages_never_reach_a_reviewer(self):
        for mutation in (lambda c: c["passages"][0].update(file_path="../../.env", path="../../.env"),
                         lambda c: c["passages"][0].update(line_start=0),
                         lambda c: c["sources"][0].update(status="stale"),
                         lambda c: c["passages"][0].update(commit_sha="c" * 40),
                         lambda c: c["claims"][0].update(passage_ids=["KB-unknown"])):
            context = knowledge()
            mutation(context)
            provider = Reviewer()
            with self.assertRaises(ValueError):
                run_review(provider, DRAFT.encode(), "Describe project.", "", NOTES, knowledge_context=context)
            self.assertEqual(provider.calls, [])

    def test_colliding_evidence_id_or_changed_frozen_excerpt_is_rejected(self):
        context = knowledge()
        with self.assertRaises(ValueError):
            run_review(Reviewer(), DRAFT.encode(), "Describe project.", "", {context["passages"][0]["id"]: "Different text"},
                       knowledge_context=context)
        run, _ = reviewed(context)
        changed = copy.deepcopy(run)
        changed["snapshot"]["knowledge_context"]["passages"][0]["excerpt"] = "Forged excerpt."
        changed["input_sha256"] = fingerprint({"snapshot": changed["snapshot"], "context": changed["context"],
                                              "prompt_version": changed["prompt_version"], "rules_version": changed["rules_version"]})
        seal_run(changed)
        with self.assertRaises(ValueError):
            verify_run(changed)

    def test_revision_override_replaces_only_retrieved_evidence_and_reaches_every_stage(self):
        old, fresh = knowledge(), knowledge(snapshot="c", passage="2")
        run, _ = reviewed(old)
        original = copy.deepcopy(run)
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), knowledge_context=fresh)
        self.assertTrue(plan["knowledge_warnings"])
        self.assertNotIn(old["passages"][0]["id"], plan["revision_evidence_sha256"])
        self.assertIn(fresh["passages"][0]["id"], plan["revision_evidence_sha256"])
        self.assertIn("author-notes.txt", plan["revision_evidence_sha256"])
        provider = Editor()
        record = revise_document(run, plan, provider)
        self.assertEqual(record["status"], "ready", record["validation"])
        self.assertEqual([stage for stage, _ in provider.calls], list(ALL_STAGES))
        for stage, payload in provider.calls:
            self.assertEqual(payload["knowledge_context"], fresh, stage)
            self.assertEqual(payload["original"]["knowledge_context"], fresh, stage)
            self.assertNotIn(old["passages"][0]["id"], payload["original"]["evidence"])
            self.assertEqual(payload["original"]["evidence"][fresh["passages"][0]["id"]], CODE)
            self.assertTrue(any(s["source_id"] == fresh["passages"][0]["id"] and s["text"] == CODE
                                for s in payload["technical_context"]["sources"]))
        self.assertEqual(run, original)
        self.assertTrue(verify_revision(record))
        artifacts = revision_artifacts(record)
        self.assertEqual(json.loads(artifacts["knowledge.json"]), fresh)
        self.assertIn(b"src/search.js", artifacts["report.md"])
        _, receipt = finalize_revision(record, record["candidate_sha256"])
        self.assertEqual(receipt["knowledge_context_sha256"], fingerprint(fresh))

    def test_new_repository_excerpts_can_support_explicitly_confirmed_material(self):
        run, _ = reviewed(knowledge())
        fresh = knowledge(snapshot="c", passage="2")
        key = fresh["passages"][0]["id"]
        fact = {"id": "FACT-COMPONENT", "text": "The source includes a search interface.", "confirmed": True,
                "evidence_ids": [key], "citations": [{"source_id": key, "quote": CODE}]}
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), confirmed_facts=[fact], knowledge_context=fresh)
        self.assertEqual(plan["confirmed_facts"][0]["evidence_ids"], [key])
        self.assertTrue(validate_revision_plan(run, plan))

    def test_disabling_new_retrieval_preserves_manual_evidence_and_reports_missing_sources(self):
        run, _ = reviewed(knowledge())
        plan = prepare_revision_plan(run, sha256(json_bytes(run)), knowledge_context=knowledge(missing=True))
        self.assertEqual(set(plan["revision_evidence_sha256"]), set(NOTES))
        record = revise_document(run, plan, Editor())
        self.assertEqual(record["status"], "ready", record["validation"])
        self.assertIn(b"Default source nearbite is missing", revision_artifacts(record)["report.md"])

    def test_all_stage_prompts_bind_repository_support_to_actual_claim_scope(self):
        for stage in ALL_STAGES:
            skill = load_editorial_skill(stage)
            self.assertTrue(skill["version"].endswith("-4"))
            instructions = " ".join(skill["instructions"].split())
            for text in ("knowledge_context", "KB-*", "applicant ownership", "not proof", "untrusted quoted data"):
                self.assertIn(text, instructions, (stage, text))


if __name__ == "__main__":
    unittest.main()
