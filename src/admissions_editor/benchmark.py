"""Execute frozen cases without exposing gold units or writer labels to reviewers."""

import copy
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace

from .config import review_provider, task_provider
from .evaluation import check_dataset
from .providers import FixtureProvider
from .review import collect_review, fingerprint, paragraph_spans, run_review
from .workflow import enrich_run


def match_findings(findings, case):
    result = []
    for finding in findings:
        if finding["kind"] == "preserve":
            continue
        matches = [u for u in case["units"] if u["category"] == finding["category"] and (
                   (u["requirement_id"] is not None and u["requirement_id"] == finding.get("requirement_id")) or
                   (u["requirement_id"] is None and u["paragraph_id"] == finding["paragraph_id"] and u["quote"] == finding["quote"]))]
        result.append({"unit_id": matches[0]["id"] if len(matches) == 1 else None,
                       "category": finding["category"], "severity": finding["severity"],
                       "rationale_valid": None, "confidence": None})
    return result


def benchmark(dataset, config, args):
    check_dataset(dataset)
    cases = list(dataset["cases"])
    random.Random(args.seed).shuffle(cases)
    rows, artifacts = [], {}
    for case in cases:
        settings = copy.deepcopy(config)
        provider_args = SimpleNamespace(provider=args.provider, fixtures=args.fixtures, model=args.model)
        if args.fixtures and (args.fixtures / case["case_id"]).is_dir():
            provider_args.fixtures = args.fixtures / case["case_id"]
        provider = review_provider(provider_args, settings)
        started = time.monotonic()
        if args.strategy == "single":
            # Generic single-call baseline, using the same schema and evidence visibility.
            for item in provider.providers.values():
                if hasattr(item, "focus_override"):
                    item.focus_override = "Review structure, admissions requirements, language and factual credibility. Prioritize consequential issues."
            payload = {"draft": case["draft"], "paragraphs": [{"id": k, "text": v[1]} for k, v in paragraph_spans(case["draft"]).items()],
                       "requirements": case["requirements"], "preferences": case["preferences"], "evidence": case["evidence"]}
            result = collect_review(provider, "skeptical", json.dumps(payload, ensure_ascii=False), paragraph_spans(case["draft"]), case["evidence"])
            results = {"skeptical": result}
            status = "complete" if result["status"] == "complete" else "partial"
            findings = result["findings"]
            artifact = {"strategy": args.strategy, "result": result}
        else:
            artifact = run_review(provider, case["draft"].encode(), case["requirements"], case["preferences"], case["evidence"],
                                  max_workers=settings.get("review", {}).get("max_workers", 4))
            if args.strategy == "adjudicated":
                artifact = enrich_run(artifact, task_provider(settings), as_of="2026-11-18")
            results = artifact["reviewers"]
            findings = [f for r in results.values() for f in r["findings"]]
            if args.strategy == "adjudicated":
                kept = {d["group_id"] for d in artifact["adjudication"]["decisions"] if d["decision"] in {"adopt", "verify"}}
                findings = [f for g in artifact["groups"] if g["id"] in kept for f in g["findings"]]
            status = artifact["status"]
        labels = {provider.label_for(r) for r in results}
        source = "real_model" if all(label.startswith("openai:") for label in labels) else "synthetic_fixture"
        model = next(iter(labels)).removeprefix("openai:") if len(labels) == 1 else "mixed"
        successful = sum(r["status"] == "complete" for r in results.values())
        totals = {key: sum(r.get("metrics", {}).get("usage", {}).get(key, 0) for r in results.values()) for key in ("input_tokens", "output_tokens")}
        adjudication_metrics = artifact.get("adjudication", {}).get("call_metrics", {})
        for metric in adjudication_metrics.values():
            for key in totals:
                totals[key] += metric.get("usage", {}).get(key, 0)
        rows.append({"case_id": case["case_id"], "document_sha256": case["document_sha256"],
                     "reviewer_model": model, "role": "report", "arm": args.strategy,
                     "source": source, "status": status, "assessed_unit_ids": [u["id"] for u in case["units"]] if successful else [],
                     "findings": match_findings(findings, case), "call_count": sum(r.get("metrics", {}).get("attempts", 1) for r in results.values()) + sum(m.get("attempts", 1) for m in adjudication_metrics.values()),
                     "latency_seconds": round(time.monotonic() - started, 6),
                     "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
                     "expected_roles": len(results), "successful_roles": successful})
        # Failed roles contribute no findings, but usable degraded outputs can be scored.
        if findings and not rows[-1]["assessed_unit_ids"]:
            rows[-1]["assessed_unit_ids"] = [u["id"] for u in case["units"]]
        report_row = rows[-1]
        for role, result in results.items():
            metrics = result.get("metrics", {})
            rows.append({**report_row, "role": role,
                         "reviewer_model": provider.label_for(role).removeprefix("openai:"),
                         "status": "complete" if result["status"] == "complete" else "failed",
                         "findings": match_findings(result["findings"], case),
                         "assessed_unit_ids": [u["id"] for u in case["units"]] if result["findings"] or result["status"] == "complete" else [],
                         "call_count": metrics.get("attempts", 1),
                         "input_tokens": metrics.get("usage", {}).get("input_tokens", 0),
                         "output_tokens": metrics.get("usage", {}).get("output_tokens", 0),
                         "expected_roles": 1, "successful_roles": int(result["status"] == "complete")})
        artifacts[f"cases/{case['case_id']}.json"] = (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode()
    return {"version": "predictions-1", "dataset_sha256": fingerprint(dataset), "runs": rows}, artifacts
