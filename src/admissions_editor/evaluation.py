"""Frozen unit-level scoring and paired, family-clustered self-review analysis."""

import random
import re
from collections import defaultdict

from .contracts import NULL_STRING, STRING, STRINGS, array, choice, obj, validate
from .review import fingerprint, sha256

UNIT = obj({"id": STRING, "label": choice("issue", "clean", "uncertain"),
            "category": choice("structure", "admissions", "language", "evidence"),
            "severity": choice("high", "medium", "low"), "paragraph_id": NULL_STRING,
            "quote": NULL_STRING, "requirement_id": NULL_STRING, "evidence_ids": STRINGS,
            "rationale": STRING, "acceptable_outcomes": STRINGS})
CASE = obj({"case_id": STRING, "family_id": STRING, "split": choice("dev", "calibration", "test"),
            "origin": choice("synthetic", "authorized_deidentified"), "draft": STRING,
            "document_sha256": STRING, "requirements": STRING, "preferences": STRING,
            "evidence": {"type": "object", "additionalProperties": STRING},
            "writer_model": NULL_STRING, "writer_provenance": choice("synthetic_assignment", "model", "human"),
            "annotation": obj({"version": STRING, "annotators": STRINGS, "adjudicator": NULL_STRING,
                               "status": choice("synthetic", "pending", "adjudicated"), "consent_reference": NULL_STRING}),
            "units": array(UNIT)})
DATASET = obj({"version": STRING, "cases": array(CASE)})
FINDING = obj({"unit_id": NULL_STRING, "category": STRING, "severity": choice("high", "medium", "low"),
               "rationale_valid": {"type": ["boolean", "null"]},
               "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1}})
PREDICTION = obj({"case_id": STRING, "document_sha256": STRING, "reviewer_model": STRING,
                  "role": STRING, "arm": STRING, "source": choice("synthetic_fixture", "real_model"),
                  "status": choice("complete", "partial", "failed"), "assessed_unit_ids": STRINGS,
                  "findings": array(FINDING), "call_count": {"type": "integer", "minimum": 0},
                  "latency_seconds": {"type": "number", "minimum": 0},
                  "input_tokens": {"type": "integer", "minimum": 0},
                  "output_tokens": {"type": "integer", "minimum": 0},
                  "expected_roles": {"type": "integer", "minimum": 1},
                  "successful_roles": {"type": "integer", "minimum": 0}})
PREDICTIONS = obj({"version": STRING, "dataset_sha256": STRING, "runs": array(PREDICTION)})


def check_dataset(dataset):
    from .review import paragraph_spans
    validate(dataset, DATASET, "Evaluation dataset")
    cases = dataset["cases"]
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique.")
    splits = defaultdict(set)
    versions = defaultdict(set)
    for case in cases:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", case["case_id"]):
            raise ValueError("Evaluation case IDs must be safe file identifiers.")
        splits[case["family_id"]].add(case["split"])
        versions[case["document_sha256"]].add(case["split"])
        if sha256(case["draft"].encode()) != case["document_sha256"]:
            raise ValueError("Evaluation document hash mismatch.")
        if case["origin"] == "authorized_deidentified" and not case["annotation"]["consent_reference"]:
            raise ValueError("Real evaluation samples need a consent reference.")
        annotation = case["annotation"]
        if annotation["status"] == "adjudicated" and (len(set(annotation["annotators"])) < 2 or
                not annotation["adjudicator"] or annotation["adjudicator"] in annotation["annotators"]):
            raise ValueError("Adjudicated gold needs two independent annotators and a distinct adjudicator.")
        units = case["units"]
        if len({u["id"] for u in units}) != len(units):
            raise ValueError("Evaluation unit IDs must be unique within a case.")
        paragraphs = {k: v[1] for k, v in paragraph_spans(case["draft"]).items()}
        for unit in units:
            if not unit["requirement_id"] and (not unit["quote"] or paragraphs.get(unit["paragraph_id"], "").count(unit["quote"]) != 1):
                raise ValueError("Gold unit needs a unique exact anchor or a missing requirement ID.")
            if set(unit["evidence_ids"]) - case["evidence"].keys():
                raise ValueError("Gold unit cites missing evidence.")
    if any(len(s) > 1 for s in splits.values()) or any(len(s) > 1 for s in versions.values()):
        raise ValueError("A draft family or identical document leaks across dataset splits.")


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def score_rows(cases, rows):
    tp = fp = fn = clean_den = clean_fp = severe_den = severe_hit = severe_success_den = severe_success_hit = 0
    emitted = duplicates = unmatched = pending_reasons = reason_invalid = severity_underestimated = 0
    clean_docs = clean_docs_fp = 0
    cost = defaultdict(float)
    calibration = [[] for _ in range(5)]
    brier = []
    for row in rows:
        case = cases[row["case_id"]]
        units = {u["id"]: u for u in case["units"]}
        assessed = set(row["assessed_unit_ids"])
        unique = {}
        for finding in row["findings"]:
            unit_id = finding["unit_id"]
            if unit_id is None:
                unmatched += 1
                emitted += 1
                continue
            key = (unit_id, finding["category"])
            if key in unique:
                duplicates += 1
                continue
            unique[key] = finding
        hits, false_points = set(), set()
        for (unit_id, category), finding in unique.items():
            unit = units[unit_id]
            if unit["label"] == "uncertain":
                continue
            emitted += 1
            valid = unit["label"] == "issue" and category == unit["category"] and finding["rationale_valid"] is not False
            if finding["rationale_valid"] is None:
                pending_reasons += 1
            if finding["rationale_valid"] is False and unit["label"] == "issue":
                reason_invalid += 1
            if valid:
                # One gold problem matches at most one emitted finding at report level.
                if unit_id not in hits:
                    tp += 1
                    hits.add(unit_id)
                    if unit["severity"] == "high" and finding["severity"] != "high":
                        severity_underestimated += 1
            else:
                fp += 1
                if unit["label"] == "clean" and category == unit["category"]:
                    false_points.add(unit_id)
            if finding["confidence"] is not None:
                confidence = finding["confidence"]
                outcome = int(valid)
                brier.append((confidence - outcome) ** 2)
                calibration[min(int(confidence * 5), 4)].append((confidence, outcome))
        positive = {u["id"] for u in units.values() if u["label"] == "issue"}
        severe = {u["id"] for u in units.values() if u["label"] == "issue" and u["severity"] == "high"}
        negatives = {u["id"] for u in units.values() if u["label"] == "clean"}
        fn += len(positive - hits)
        severe_den += len(severe)
        severe_hit += len(severe & hits)
        if row["status"] == "complete":
            severe_success_den += len(severe)
            severe_success_hit += len(severe & hits)
        clean_den += len(negatives & assessed)
        clean_fp += len(false_points)
        if not positive and negatives & assessed:
            clean_docs += 1
            clean_docs_fp += bool(false_points)
        for key in ("call_count", "latency_seconds", "input_tokens", "output_tokens", "expected_roles", "successful_roles"):
            cost[key] += row[key]
    return {"run_count": len(rows), "case_count": len({r["case_id"] for r in rows}),
            "family_count": len({cases[r["case_id"]]["family_id"] for r in rows}),
            "precision_frozen": ratio(tp, tp + fp + unmatched), "recall_all": ratio(tp, tp + fn),
            "false_positive_rate": ratio(clean_fp, clean_den),
            "clean_document_false_positive_rate": ratio(clean_docs_fp, clean_docs),
            "serious_recall_all": ratio(severe_hit, severe_den),
            "serious_recall_successful": ratio(severe_success_hit, severe_success_den),
            "serious_miss_rate_all": ratio(severe_den - severe_hit, severe_den),
            "severity_underestimated": severity_underestimated,
            "correct_location_invalid_reason": reason_invalid,
            "unmatched_alerts_needing_annotation": unmatched,
            "matched_alerts_needing_reason_review": pending_reasons,
            "duplicate_alerts": duplicates, "deduplicated_alerts": emitted,
            "availability": ratio(sum(r["status"] == "complete" for r in rows), len(rows)),
            "reviewer_coverage": ratio(int(cost["successful_roles"]), int(cost["expected_roles"])),
            "brier_score": {"value": sum(brier) / len(brier) if brier else None, "sample_count": len(brier)},
            "calibration_bins": [{"range": [i / 5, (i + 1) / 5], "sample_count": len(values),
                                  "mean_confidence": sum(v[0] for v in values) / len(values) if values else None,
                                  "empirical_precision": sum(v[1] for v in values) / len(values) if values else None}
                                 for i, values in enumerate(calibration)],
            "cost": dict(cost), "monetary_cost": None}


def paired_bias(cases, rows, bootstrap=500, seed=17):
    pairs = []
    by_case = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    for case_id, candidates in by_case.items():
        case = cases[case_id]
        writer = case["writer_model"]
        if writer is None:
            continue
        same = [r for r in candidates if r["reviewer_model"] == writer]
        cross = [r for r in candidates if r["reviewer_model"] != writer and r["reviewer_model"] != "mixed"]
        # Match within strategy/role/source. Average repeated runs per arm inside each draft.
        strata = {(r["arm"], r["role"], r["source"]) for r in same}
        for stratum in strata:
            left = [r for r in same if (r["arm"], r["role"], r["source"]) == stratum]
            right = [r for r in cross if (r["arm"], r["role"], r["source"]) == stratum]
            if not left or not right:
                continue
            l = score_rows(cases, left)
            r = score_rows(cases, right)
            lm, rm = l["serious_miss_rate_all"]["value"], r["serious_miss_rate_all"]["value"]
            if lm is None or rm is None:
                continue
            lf, rf = l["false_positive_rate"]["value"], r["false_positive_rate"]["value"]
            pairs.append({"case_id": case_id, "family_id": case["family_id"], "writer_model": writer,
                          "arm": stratum[0], "role": stratum[1], "source": stratum[2],
                          "serious_miss_delta": lm - rm,
                          "fpr_delta": lf - rf if lf is not None and rf is not None else None,
                          "writer_provenance": case["writer_provenance"], "data_origin": case["origin"],
                          "annotation_status": case["annotation"]["status"], "split": case["split"]})

    def mean_balanced(items, key):
        per_writer = defaultdict(list)
        for item in items:
            if item[key] is not None:
                per_writer[item["writer_model"]].append(item[key])
        means = [sum(v) / len(v) for v in per_writer.values()]
        return sum(means) / len(means) if means else None

    summaries = []
    def stratum_key(p):
        return (p["arm"], p["role"], p["source"], p["data_origin"], p["annotation_status"], p["split"])

    for stratum in sorted({stratum_key(p) for p in pairs}):
        items = [p for p in pairs if stratum_key(p) == stratum]
        family_ids = sorted({p["family_id"] for p in items})
        rng = random.Random(seed)
        samples = []
        if len(family_ids) >= 2:
            for _ in range(bootstrap):
                chosen = rng.choices(family_ids, k=len(family_ids))
                sampled = [p for family in chosen for p in items if p["family_id"] == family]
                if {p["writer_model"] for p in sampled} != {p["writer_model"] for p in items}:
                    continue
                samples.append(mean_balanced(sampled, "serious_miss_delta"))
        samples.sort()
        per_writer = {}
        for writer in sorted({p["writer_model"] for p in items}):
            subset = [p for p in items if p["writer_model"] == writer]
            per_writer[writer] = {"paired_drafts": len(subset),
                                  "serious_miss_delta": mean_balanced(subset, "serious_miss_delta")}
        summaries.append({"arm": stratum[0], "role": stratum[1], "source": stratum[2],
                          "data_origin": stratum[3], "annotation_status": stratum[4], "split": stratum[5],
                          "paired_drafts": len(items), "family_count": len(family_ids), "by_writer": per_writer,
                          "equal_weight_writer_serious_miss_delta": mean_balanced(items, "serious_miss_delta"),
                          "equal_weight_writer_fpr_delta": mean_balanced(items, "fpr_delta"),
                          "family_bootstrap_95_interval": [samples[int((len(samples) - 1) * .025)], samples[int((len(samples) - 1) * .975)]] if samples else None,
                          "bootstrap_resamples": len(samples), "seed": seed,
                          "evidence_status": "insufficient" if len(family_ids) < 30 or len(per_writer) < 2 else "requires_human_interpretation",
                          "simulated_writer_assignment": any(p["writer_provenance"] == "synthetic_assignment" for p in items)})
    return {"definition": "same-model serious miss rate minus cross-model serious miss rate; equal weight per writer",
            "groups": summaries, "pairs": pairs}


def family_intervals(cases, rows, bootstrap, seed):
    families = sorted({cases[r["case_id"]]["family_id"] for r in rows})
    keys = ("false_positive_rate", "serious_recall_all", "precision_frozen")
    values = {key: [] for key in keys}
    rng = random.Random(seed)
    if len(families) >= 2:
        by_family = {f: [r for r in rows if cases[r["case_id"]]["family_id"] == f] for f in families}
        for _ in range(bootstrap):
            sampled = [r for family in rng.choices(families, k=len(families)) for r in by_family[family]]
            scores = score_rows(cases, sampled)
            for key in keys:
                if scores[key]["value"] is not None:
                    values[key].append(scores[key]["value"])
    result = {"method": "family cluster percentile bootstrap", "family_count": len(families), "seed": seed,
              "requested_resamples": bootstrap, "evidence_status": "insufficient" if len(families) < 30 else "requires_human_interpretation"}
    for key, items in values.items():
        items.sort()
        result[key] = {"usable_resamples": len(items), "interval_95":
                       [items[int((len(items) - 1) * .025)], items[int((len(items) - 1) * .975)]] if items else None}
    return result


def evaluate(dataset, predictions, bootstrap=500, seed=17):
    check_dataset(dataset)
    validate(predictions, PREDICTIONS, "Evaluation predictions")
    if predictions["dataset_sha256"] != fingerprint(dataset):
        raise ValueError("Predictions refer to a different frozen dataset version.")
    if not 1 <= bootstrap <= 10000:
        raise ValueError("bootstrap must be 1..10000.")
    cases = {c["case_id"]: c for c in dataset["cases"]}
    coverage = defaultdict(set)
    for row in predictions["runs"]:
        if row["case_id"] not in cases or row["document_sha256"] != cases[row["case_id"]]["document_sha256"]:
            raise ValueError("Prediction case or document version mismatch.")
        units = {u["id"] for u in cases[row["case_id"]]["units"]}
        if set(row["assessed_unit_ids"]) - units or any(f["unit_id"] is not None and f["unit_id"] not in row["assessed_unit_ids"] for f in row["findings"]):
            raise ValueError("Prediction refers to an unknown/unassessed evaluation unit.")
        if row["successful_roles"] > row["expected_roles"]:
            raise ValueError("Invalid role availability counts.")
        coverage[(row["reviewer_model"], row["role"], row["arm"], row["source"])].add(row["case_id"])
    if any(ids != set(cases) for ids in coverage.values()):
        raise ValueError("Each prediction configuration must cover the frozen dataset. Record explicit failed rows for missing calls.")
    groups = defaultdict(list)
    for row in predictions["runs"]:
        c = cases[row["case_id"]]
        key = (c["origin"], c["annotation"]["status"], c["split"], row["source"], row["arm"], row["role"], row["reviewer_model"])
        groups[key].append(row)
    metrics = [{"data_origin": key[0], "annotation_status": key[1], "split": key[2], "prediction_source": key[3],
                "arm": key[4], "role": key[5], "reviewer_model": key[6], **score_rows(cases, rows),
                "family_intervals": family_intervals(cases, rows, bootstrap, seed)}
               for key, rows in sorted(groups.items())]
    return {"version": "evaluation-1", "dataset_version": dataset["version"], "dataset_sha256": fingerprint(dataset),
            "case_count": len(cases), "family_count": len({c["family_id"] for c in cases.values()}),
            "unit_count": sum(len(c["units"]) for c in cases.values()), "prediction_runs": len(predictions["runs"]),
            "groups": metrics, "self_review_bias": paired_bias(cases, predictions["runs"], bootstrap, seed),
            "pending_human_validation": ["two independent annotations and third-person adjudication",
                 "semantic matching and rationale validity for real model findings", "author acceptance and voice preservation",
                 "fact verification confusion matrix and revision regression on authorized material"],
            "limitations": ["Synthetic fixtures test scoring and workflow only; they do not measure real model quality.",
                 "Null reason judgments yield provisional matching scores, not validated precision/recall.",
                 "Unmatched alerts remain pending annotation and count against frozen precision.",
                 "Missing denominators are null, never zero; API failures remain in full-sample serious recall.",
                 "Ratios are grouped by source/configuration; repeated role outputs are not independent drafts."]}


def render_evaluation(report):
    lines = ["# Evaluation report", "", f"Dataset: {report['dataset_version']}; {report['case_count']} cases, {report['family_count']} families, {report['unit_count']} units.", "",
             "| Source / annotation / model / role | n runs | FPR | Serious recall (all) | Serious recall (successful) |", "| --- | ---: | --- | --- | --- |"]

    def cell(metric):
        return f"{metric['value']:.3f} ({metric['numerator']}/{metric['denominator']})" if metric["value"] is not None else f"N/A ({metric['numerator']}/{metric['denominator']})"

    for g in report["groups"]:
        lines.append(f"| {g['prediction_source']} / {g['annotation_status']} / {g['reviewer_model']} / {g['role']} | {g['run_count']} | {cell(g['false_positive_rate'])} | {cell(g['serious_recall_all'])} | {cell(g['serious_recall_successful'])} |")
    lines += ["", "Self-review: same-model minus cross-model serious miss rate; see metrics.json for paired counts and family bootstrap intervals.", ""]
    for group in report["self_review_bias"]["groups"]:
        lines.append(f"- {group['source']}: delta={group['equal_weight_writer_serious_miss_delta']}; n={group['paired_drafts']} paired drafts, {group['family_count']} families; {group['evidence_status']}; simulated={group['simulated_writer_assignment']}")
    if not report["self_review_bias"]["groups"]:
        lines.append("No eligible same/cross-model pairs; self-review bias is not computable.")
    lines += ["", "Real model runs: " + ("present; inspect source and provisional reason checks." if any(g["prediction_source"] == "real_model" for g in report["groups"]) else "not executed."), ""]
    lines += [f"- {item}" for item in report["limitations"] + report["pending_human_validation"]]
    return "\n".join(lines) + "\n"
