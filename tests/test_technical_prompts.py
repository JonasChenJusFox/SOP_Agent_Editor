"""Technical editing policy reaches each application stage without new services."""

import copy
import json
import os
import unittest
from unittest.mock import patch

from admissions_editor.editorial_skills import STAGES, editorial_manifest, load_editorial_skill
from admissions_editor.providers import OpenAIProvider
from admissions_editor.revision_schema import REVISION_SCHEMAS


def completed(value):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": json.dumps(value)}]}]}


class TechnicalPromptTest(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-technical-test-key"})
    def test_each_writer_receives_technical_sources_and_glossary_unchanged(self):
        context = {"check_required_claim_ids": ["TECH-001"],
                   "claims": [{"id": "TECH-001", "quote": "I built the retrieval interface; I did not fine-tune the model.",
                               "source_type": "draft", "source_id": "P001"}],
                   "terms": [{"term": "retrieval", "category": "method"}, {"term": "embeddings", "category": "method"}],
                   "glossary": [{"term": "Atlas", "definition": "Our interface around a retrieval API.", "aliases": []}]}
        payload = {"original": {"draft": context["claims"][0]["quote"], "evidence": {
            "notes.txt": "The applicant built the interface; another team developed the model."}},
            "technical_context": context, "current_essay": context["claims"][0]["quote"]}
        frozen = copy.deepcopy(payload)
        output = {"essay": payload["current_essay"], "addressed_finding_ids": [], "used_fact_ids": [],
                  "unresolved_questions": [], "technical_flags": []}
        for stage in ("sop_revise", "polish", "humanize"):
            with self.subTest(stage=stage):
                provider = OpenAIProvider("synthetic-model", retries=0)
                with patch.object(provider, "_request", return_value=completed(output)) as request:
                    self.assertEqual(provider.task(stage, payload, REVISION_SCHEMAS[stage]), output)
                wire = request.call_args.args[0]
                self.assertEqual(json.loads(wire["input"]), frozen)
                self.assertFalse(wire["store"])
                self.assertNotIn("tools", wire)
                self.assertNotIn("conversation", wire)
                instructions = wire["instructions"]
                for term in ("technical_context", "technical_flags", "retrieval", "ranking", "embeddings", "fine-tuning"):
                    self.assertIn(term, instructions)
                self.assertIn("retain the original", instructions)
                self.assertIn("never evidence", instructions)
                self.assertIn("Do not apply the risky change", instructions)
        self.assertEqual(payload, frozen)

    def test_humanizer_does_not_treat_specialization_as_ai_writing(self):
        instructions = load_editorial_skill("humanize")["instructions"]
        self.assertIn("A legitimate specialized technical term is not an AI-writing", instructions)
        self.assertIn("Do not erase", instructions)
        self.assertIn("building an interface versus developing the underlying model", instructions)
        self.assertIn("Do not output detector scores", instructions)
        self.assertIn("read-only", instructions)

    def test_final_technical_check_is_read_only_and_cannot_skip_draft_claims(self):
        self.assertEqual(STAGES, ("sop_revise", "polish", "humanize", "verify_revision"))
        instructions = load_editorial_skill("verify_revision")["instructions"]
        for field in ("technical_checks", "technical_context.check_required_claim_ids", "original_quote", "final_quote",
                      "source_type", "source_id", "source_quote", "reason"):
            self.assertIn(field, instructions)
        self.assertIn("exactly once", instructions)
        self.assertIn("original_quote must equal that extracted claim's quote", instructions)
        self.assertIn("not_used only for an optional evidence-only claim", instructions)
        self.assertIn("with null final_quote", instructions)
        self.assertIn("Do not label a changed claim preserved", instructions)
        self.assertIn("new or missed technical claims", instructions)
        self.assertIn("Do not rewrite", instructions)
        self.assertIn("cannot serve as factual evidence", instructions)

    def test_new_policies_have_versioned_packaged_metadata_and_fixed_upstream_sources(self):
        expected = {"sop_revise": "sop-revise-4", "polish": "polish-4", "humanize": "humanize-4",
                    "verify_revision": "verify-revision-4"}
        metadata = editorial_manifest()
        self.assertEqual({entry["stage"]: entry["version"] for entry in metadata}, expected)
        for entry in metadata:
            self.assertNotIn("instructions", entry)
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertEqual(len(entry["manifest_sha256"]), 64)
            for source in entry["references"]:
                self.assertIn(source["commit"], {"5522a872b44272738160b9415487421641807ced",
                                                "214981fe02326f27b0fc8790d00eb4b731607073"})
                self.assertEqual(source["license"], "MIT")


if __name__ == "__main__":
    unittest.main()
