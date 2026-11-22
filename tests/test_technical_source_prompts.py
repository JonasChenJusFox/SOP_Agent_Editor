"""Declared applicant facts and contextual degree names reach bounded stages."""

import copy
import json
import os
import unittest
from unittest.mock import patch

from admissions_editor.editorial_skills import STAGES, editorial_manifest, load_editorial_skill
from admissions_editor.providers import OpenAIProvider
from admissions_editor.revision_schema import REVISION_SCHEMAS


class TechnicalSourcePromptTest(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-confirmed-facts-key"})
    def test_three_writers_receive_exact_confirmed_sources_without_becoming_independent_proof(self):
        fact = {"id": "FACT-INTERFACE", "text": "I implemented the backend retrieval interface; my team developed the model.", "confirmed": True}
        context = {"sources": [{"source_type": "confirmed_fact", "source_id": fact["id"], "text": fact["text"]}],
                   "terminology_context": {"requirements": "SM in Data Science", "scope": "Terminology only."},
                   "check_required_claim_ids": [], "glossary": []}
        payload = {"technical_context": context, "confirmed_facts": [fact], "current_essay": "I worked with my team."}
        frozen = copy.deepcopy(payload)
        output = {"essay": fact["text"], "addressed_finding_ids": [], "used_fact_ids": [fact["id"]],
                  "unresolved_questions": [], "technical_flags": []}
        response = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": json.dumps(output)}]}]}
        for stage in STAGES[:3]:
            with self.subTest(stage=stage):
                provider = OpenAIProvider("synthetic-model", retries=0)
                with patch.object(provider, "_request", return_value=response) as request:
                    self.assertEqual(provider.task(stage, payload, REVISION_SCHEMAS[stage]), output)
                wire = request.call_args.args[0]
                self.assertEqual(json.loads(wire["input"]), frozen)
                self.assertFalse(wire["store"])
                self.assertNotIn("conversation", wire)
                instructions = wire["instructions"]
                self.assertIn("confirmed_fact", instructions)
                self.assertIn("exact text and stated scope", instructions)
                self.assertIn("Confirmation does not make the", instructions)
                self.assertIn("independently verified", instructions)
                self.assertIn("decision reasons are not factual authority", instructions)
                self.assertIn("glossary definitions", instructions)
        self.assertEqual(payload, frozen)

    def test_degree_terminology_does_not_authorize_a_qualification_or_contribution(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                instructions = load_editorial_skill(stage)["instructions"]
                self.assertIn("SM, S.M., MS, or M.Sc.", instructions)
                self.assertIn("terminology-only context separate", instructions)
                self.assertIn("does not establish that the applicant earned that degree", instructions)
                self.assertIn("or achieved a measured result", instructions)

    def test_final_verifier_cites_exact_declared_fact_ids_and_checks_real_scope(self):
        instructions = load_editorial_skill("verify_revision")["instructions"]
        self.assertIn("Allowed source_type values are draft, evidence, and confirmed_fact", instructions)
        self.assertIn("exact confirmed applicant fact/answer ID", instructions)
        self.assertIn("confirmed interface contribution cannot authorize development", instructions)
        self.assertIn("underlying model, sole ownership, or a new result", instructions)
        self.assertIn("Distinguish author-confirmed statements", instructions)
        self.assertIn("Keep verification read-only", instructions)
        self.assertEqual(STAGES, ("sop_revise", "polish", "humanize", "verify_revision"))

    def test_version_four_metadata_preserves_upstream_license_and_source_pins(self):
        manifest = editorial_manifest()
        self.assertEqual(len(manifest), 4)
        self.assertTrue(all(entry["version"].endswith("-4") for entry in manifest))
        for entry in manifest:
            self.assertEqual(len(entry["sha256"]), 64)
            for source in entry["references"]:
                self.assertEqual(source["license"], "MIT")
                self.assertIn(source["commit"], {"5522a872b44272738160b9415487421641807ced",
                                                "214981fe02326f27b0fc8790d00eb4b731607073"})


if __name__ == "__main__":
    unittest.main()
