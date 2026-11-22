import copy
import json
import unittest
from pathlib import Path

from admissions_editor.annotations import agreement, annotation_pack
from admissions_editor.benchmark import match_findings

ROOT = Path(__file__).resolve().parents[1]


class AnnotationTest(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads((ROOT / "examples/evaluation/dataset.json").read_text())

    def test_blind_pack_excludes_writer_gold_and_other_annotators(self):
        pack, labels = annotation_pack(self.dataset, "annotator-a")
        text = json.dumps(pack)
        self.assertNotIn("writer_model", text)
        self.assertNotIn("acceptable_outcomes", text)
        self.assertNotIn("rationale", text)
        self.assertNotIn('"label"', text)
        self.assertTrue(all(l["label"] is None for l in labels["labels"]))
        self.assertEqual(len(labels["labels"]), 32)

    def test_agreement_uses_independent_labels_and_retains_disagreements(self):
        _, left = annotation_pack(self.dataset, "annotator-a")
        _, right = annotation_pack(self.dataset, "annotator-b")
        left["source"] = right["source"] = "synthetic"
        for index, (a, b) in enumerate(zip(left["labels"], right["labels"])):
            a["label"] = b["label"] = "issue" if index % 2 else "clean"
            a["severity"] = b["severity"] = "high"
        right["labels"][0]["label"] = "uncertain"
        result = agreement(self.dataset, [left, right])
        pair = result["pairs"][0]
        self.assertEqual(pair["label_agreement"]["value"], 31 / 32)
        self.assertEqual(len(pair["disagreements_for_third_annotator"]), 1)
        self.assertIsNotNone(pair["cohen_kappa"])

    def test_same_annotator_duplicates_and_mixed_sources_are_rejected(self):
        _, left = annotation_pack(self.dataset, "annotator-a")
        with self.assertRaises(ValueError):
            agreement(self.dataset, [left, left])
        right = copy.deepcopy(left)
        right["source"] = "synthetic"
        with self.assertRaises(ValueError):
            agreement(self.dataset, [left, right])

    def test_missing_requirement_matches_id_without_inventing_a_source_quote(self):
        case = next(c for c in self.dataset["cases"] if c["case_id"] == "SYN-03-issue")
        result = match_findings([{"kind": "issue", "category": "admissions", "paragraph_id": "P002",
                                 "quote": "I want graduate training.", "requirement_id": "WHY_PROGRAM", "severity": "high"}], case)
        self.assertEqual(result[0]["unit_id"], "target")


if __name__ == "__main__":
    unittest.main()
