import copy
import json
import unittest
from pathlib import Path

from admissions_editor.evaluation import check_dataset, evaluate, score_rows
from admissions_editor.review import fingerprint

ROOT = Path(__file__).resolve().parents[1]


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads((ROOT / "examples/evaluation/dataset.json").read_text())
        self.predictions = json.loads((ROOT / "examples/evaluation/predictions.json").read_text())

    def test_synthetic_scores_report_source_denominators_and_simulated_bias(self):
        report = evaluate(self.dataset, self.predictions, bootstrap=100)
        self.assertEqual(report["case_count"], 16)
        self.assertEqual(report["family_count"], 8)
        self.assertTrue(all(g["prediction_source"] == "synthetic_fixture" for g in report["groups"]))
        by_model = {g["reviewer_model"]: g for g in report["groups"]}
        self.assertEqual(by_model["A"]["false_positive_rate"]["denominator"], 23)
        self.assertEqual(by_model["B"]["false_positive_rate"], {"numerator": 1, "denominator": 24, "value": 1 / 24})
        bias = report["self_review_bias"]["groups"][0]
        self.assertAlmostEqual(bias["equal_weight_writer_serious_miss_delta"], 2 / 3)
        self.assertTrue(bias["simulated_writer_assignment"])
        self.assertEqual(bias["evidence_status"], "insufficient")
        self.assertEqual(bias["family_count"], 6)
        self.assertIsNotNone(bias["family_bootstrap_95_interval"])

    def test_failed_calls_remain_in_full_serious_recall_but_not_successful_subset(self):
        case = self.dataset["cases"][0]
        row = {**self.predictions["runs"][0], "status": "failed", "findings": [], "assessed_unit_ids": [], "successful_roles": 0}
        result = score_rows({case["case_id"]: case}, [row])
        self.assertEqual(result["serious_recall_all"]["value"], 0)
        self.assertIsNone(result["serious_recall_successful"]["value"])
        self.assertIsNone(result["false_positive_rate"]["value"])

    def test_fpr_uses_known_clean_units_not_alert_count(self):
        cases = {c["case_id"]: c for c in self.dataset["cases"]}
        row = copy.deepcopy(next(r for r in self.predictions["runs"] if r["findings"] and r["findings"][0]["rationale_valid"] is False))
        result = score_rows(cases, [row])
        self.assertEqual(result["false_positive_rate"]["value"], .5)
        self.assertEqual(result["precision_frozen"]["value"], 0)

    def test_duplicates_match_one_gold_problem_once(self):
        row = copy.deepcopy(next(r for r in self.predictions["runs"] if r["findings"] and r["findings"][0]["rationale_valid"] is True))
        row["findings"] *= 3
        result = score_rows({c["case_id"]: c for c in self.dataset["cases"]}, [row])
        self.assertEqual(result["precision_frozen"]["numerator"], 1)
        self.assertEqual(result["duplicate_alerts"], 2)

    def test_wrong_reason_is_not_a_valid_detection(self):
        row = copy.deepcopy(next(r for r in self.predictions["runs"] if r["findings"] and r["findings"][0]["rationale_valid"] is True))
        row["findings"][0]["rationale_valid"] = False
        result = score_rows({c["case_id"]: c for c in self.dataset["cases"]}, [row])
        self.assertEqual(result["recall_all"]["value"], 0)
        self.assertEqual(result["correct_location_invalid_reason"], 1)

    def test_family_split_leakage_and_wrong_document_versions_are_rejected(self):
        self.dataset["cases"][1]["split"] = "dev"
        with self.assertRaises(ValueError):
            check_dataset(self.dataset)
        self.dataset["cases"][1]["split"] = "test"
        self.predictions["runs"][0]["document_sha256"] = "old-version"
        with self.assertRaises(ValueError):
            evaluate(self.dataset, self.predictions)

    def test_unmatched_findings_stay_pending_and_in_frozen_precision_denominator(self):
        row = copy.deepcopy(next(r for r in self.predictions["runs"] if r["findings"]))
        row["findings"][0]["unit_id"] = None
        result = score_rows({c["case_id"]: c for c in self.dataset["cases"]}, [row])
        self.assertEqual(result["unmatched_alerts_needing_annotation"], 1)
        self.assertEqual(result["precision_frozen"]["value"], 0)

    def test_omitted_cases_cannot_disappear_from_full_sample_recall(self):
        self.predictions["runs"].pop()
        with self.assertRaises(ValueError):
            evaluate(self.dataset, self.predictions)


if __name__ == "__main__":
    unittest.main()
