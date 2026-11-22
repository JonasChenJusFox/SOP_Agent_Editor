import copy
import unittest

from admissions_editor.facts import automatic_claims, build_ledger


class FactLedgerTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"draft": "I built 20 tools.", "paragraphs": [{"id": "P001", "text": "I built 20 tools."}],
                         "evidence": {"notes.txt": "The author built 20 tools. The team measured no outcome."}}
        self.claim = {"id": "C-1", "paragraph_id": "P001", "quote": "I built 20 tools.",
                      "type": "personal_contribution", "subject": "author", "action_or_result": "built tools",
                      "date": None, "value": "20", "unit": "tools", "scope": "project-a",
                      "citations": [{"source_id": "notes.txt", "quote": "The author built 20 tools.",
                                     "relation": "supports", "scope": "project-a"}], "confirmed_by": "synthetic-annotator"}
        self.metadata = {"notes.txt": {"type": "team_record", "scope": "project-a", "retrieved_at": "2026-11-18"}}

    def ledger(self, claim=None, metadata=None):
        return build_ledger(self.snapshot, {"claims": [claim or self.claim]}, metadata or self.metadata, "2026-11-18")

    def test_supported_claim_retains_exact_evidence_hash_scope_and_attribution(self):
        result = self.ledger()
        claim = result["claims"][0]
        self.assertEqual(claim["status"], "supported")
        self.assertEqual(claim["verification_method"], "attributed_human_mapping")
        self.assertEqual(claim["citations"][0]["quote"], "The author built 20 tools.")
        self.assertEqual(claim["citations"][0]["source_sha256"], result["sources"]["notes.txt"]["content_sha256"])

    def test_evidence_existence_or_unattributed_mapping_does_not_establish_support(self):
        claim = {**self.claim, "confirmed_by": None}
        self.assertEqual(self.ledger(claim)["claims"][0]["status"], "pending_evidence")
        self.assertEqual(self.ledger({**self.claim, "citations": []})["claims"][0]["status"], "pending_evidence")

    def test_partial_and_direct_conflict_are_distinct(self):
        for relation, status in (("partial", "partially_supported"), ("contradicts", "source_conflict")):
            claim = copy.deepcopy(self.claim)
            claim["citations"][0]["relation"] = relation
            self.assertEqual(self.ledger(claim)["claims"][0]["status"], status)

    def test_expired_or_different_scope_source_cannot_establish_support(self):
        expired = {"notes.txt": {**self.metadata["notes.txt"], "valid_until": "2025-01-01"}}
        result = self.ledger(metadata=expired)["claims"][0]
        self.assertEqual(result["status"], "pending_evidence")
        self.assertTrue(result["citations"][0]["expired"])
        different = {"notes.txt": {**self.metadata["notes.txt"], "scope": "different-campus"}}
        self.assertEqual(self.ledger(metadata=different)["claims"][0]["status"], "pending_evidence")
        future = {"notes.txt": {**self.metadata["notes.txt"], "retrieved_at": "2027-01-01"}}
        self.assertEqual(self.ledger(metadata=future)["claims"][0]["status"], "pending_evidence")

    def test_repeated_fact_sentences_use_a_wider_unique_anchor(self):
        text = "I built 20 tools. I built 20 tools."
        self.snapshot["draft"] = text
        self.snapshot["paragraphs"][0]["text"] = text
        result = build_ledger(self.snapshot, as_of="2026-11-18")
        self.assertTrue(result["claims"])
        self.assertTrue(all(c["quote"] == text for c in result["claims"]))

    def test_invalid_evidence_excerpt_or_claim_anchor_is_rejected(self):
        claim = copy.deepcopy(self.claim)
        claim["citations"][0]["quote"] = "Invented supporting evidence."
        with self.assertRaises(ValueError):
            self.ledger(claim)
        with self.assertRaises(ValueError):
            self.ledger({**self.claim, "quote": "Not in the manuscript."})

    def test_inventory_separates_numbers_and_personal_contribution_without_inventing_verification(self):
        claims = automatic_claims(self.snapshot)
        self.assertEqual({c["type"] for c in claims}, {"personal_contribution", "number"})
        self.assertTrue(all(c["confirmed_by"] is None for c in claims))


if __name__ == "__main__":
    unittest.main()
