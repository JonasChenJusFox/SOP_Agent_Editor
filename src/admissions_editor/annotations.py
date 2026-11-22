"""Blind human annotation packs and pre-adjudication agreement."""

from collections import Counter
from itertools import combinations

from .contracts import STRING, STRINGS, array, obj, validate
from .evaluation import check_dataset, ratio
from .review import fingerprint

LABEL = obj({"case_id": STRING, "unit_id": STRING, "annotator": STRING,
             "label": {"type": ["string", "null"], "enum": ["issue", "clean", "uncertain", None]},
             "severity": {"type": ["string", "null"], "enum": ["high", "medium", "low", None]},
             "evidence_ids": STRINGS, "rationale": STRING})
ANNOTATIONS = obj({"version": STRING, "dataset_sha256": STRING,
                   "source": {"type": "string", "enum": ["synthetic", "human"]}, "labels": array(LABEL)})


def annotation_pack(dataset, annotator):
    check_dataset(dataset)
    if not annotator.strip():
        raise ValueError("Annotator ID is required.")
    cases = [{k: c[k] for k in ("case_id", "family_id", "document_sha256", "draft", "requirements", "preferences", "evidence")}
             for c in dataset["cases"]]
    for original, blind in zip(dataset["cases"], cases):
        blind["units"] = [{k: u[k] for k in ("id", "paragraph_id", "quote", "requirement_id", "category")}
                          for u in original["units"]]
    response = {"version": "annotation-1", "dataset_sha256": fingerprint(dataset), "source": "human",
                "labels": [{"case_id": c["case_id"], "unit_id": u["id"], "annotator": annotator,
                            "label": None, "severity": None, "evidence_ids": [], "rationale": ""}
                           for c in dataset["cases"] for u in c["units"]]}
    return {"instructions": "Read the blind cases independently. Fill labels.json without model output, writer identity or another annotator's labels.",
            "cases": cases}, response


def agreement(dataset, annotation_files):
    check_dataset(dataset)
    units = {(c["case_id"], u["id"]): c for c in dataset["cases"] for u in c["units"]}
    annotations = {}
    sources = set()
    for data in annotation_files:
        validate(data, ANNOTATIONS, "Human annotations")
        if data["dataset_sha256"] != fingerprint(dataset):
            raise ValueError("Annotation dataset version mismatch.")
        sources.add(data["source"])
        for label in data["labels"]:
            key = (label["case_id"], label["unit_id"])
            if key not in units or set(label["evidence_ids"]) - units[key]["evidence"].keys():
                raise ValueError("Unknown annotation unit or evidence ID.")
            full_key = (*key, label["annotator"])
            if full_key in annotations:
                raise ValueError("Duplicate annotation by the same annotator.")
            annotations[full_key] = label
    if len(sources) > 1:
        raise ValueError("Report synthetic and human annotation agreement separately.")
    results = []
    for left, right in combinations(sorted({k[2] for k in annotations}), 2):
        pairs = [(annotations[(*key, left)], annotations[(*key, right)]) for key in units
                 if (*key, left) in annotations and (*key, right) in annotations and
                 annotations[(*key, left)]["label"] is not None and annotations[(*key, right)]["label"] is not None]
        n = len(pairs)
        a, b = Counter(p[0]["label"] for p in pairs), Counter(p[1]["label"] for p in pairs)
        observed = sum(x["label"] == y["label"] for x, y in pairs) / n if n else None
        chance = sum(a[label] * b[label] for label in ("issue", "clean", "uncertain")) / (n * n) if n else None
        results.append({"annotators": [left, right], "paired_units": n,
                        "label_agreement": ratio(sum(x["label"] == y["label"] for x, y in pairs), n),
                        "cohen_kappa": (observed - chance) / (1 - chance) if n and chance != 1 else None,
                        "disagreements_for_third_annotator": [{"case_id": x["case_id"], "unit_id": x["unit_id"]}
                                                             for x, y in pairs if x["label"] != y["label"] or x["severity"] != y["severity"]]})
    return {"version": "agreement-1", "source": next(iter(sources), None), "dataset_sha256": fingerprint(dataset),
            "pairs": results, "note": "Raw independent labels only. Third-person adjudication must update a new frozen dataset version."}
