"""Verify cloud full-essay revision/acceptance using synthetic fixtures only."""

import argparse
import hashlib
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("runs/verification/cloud-full-revision.json"))
    parser.add_argument("--glossary", type=Path, help="Optional terminology JSON to bind into the synthetic revision plan.")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        headers = {"User-Agent": "AdmissionsEditorRevisionQA/1", "Origin": base}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with client.open(req, timeout=30) as response:
            return response.read()

    def api(path, body=None):
        return json.loads(request(path, body))

    def wait(path):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            job = api(path)
            if job["state"] in {"complete", "partial", "failed"}:
                return job
            time.sleep(1)
        raise TimeoutError("Timed out waiting for cloud synthetic workflow")

    def expect_conflict(path, body):
        try:
            api(path, body)
        except urllib.error.HTTPError as error:
            assert error.code == 409, error.code
            return
        raise AssertionError("Stale operation was accepted")

    api("/api/session", {"password": args.password_file.read_text().strip()})
    config = api("/api/config")
    knowledge_status = config.get("knowledge_status", {})
    assert knowledge_status.get("status") == "missing", "Native cloud must report inaccessible local default sources."
    assert not knowledge_status.get("passages")
    skills = api("/api/skills")["skills"]
    demo = api("/api/revision-demo")
    if args.glossary:
        demo["plan_input"]["glossary"] = json.loads(args.glossary.read_text())
    source_id = api("/api/reviews", {"mode": "demo", "input": demo["input"]})["id"]
    source_url = "/api/reviews/" + source_id
    source = wait(source_url)
    assert source["state"] == "complete", source
    source_text = request(source_url + "/files/review/run.json")
    run_hash = hashlib.sha256(source_text).hexdigest()
    plan = api(source_url + "/revision-plan", {"run_sha256": run_hash, **demo["plan_input"]})["plan"]
    if args.glossary:
        assert plan["glossary"] == demo["plan_input"]["glossary"]
    expect_conflict(source_url + "/revisions", {"run_sha256": "stale", "plan": plan})
    revision_id = api(source_url + "/revisions", {"run_sha256": run_hash, "plan": plan})["id"]
    revision_url = "/api/revisions/" + revision_id
    revision = wait(revision_url)
    assert revision["state"] == "complete", revision
    result = revision["result"]
    assert result["status"] == "ready" and result["validation"]["passed"] and not result["blockers"]
    record = api(revision_url + "/files/revision.json")
    from admissions_editor.revision import verify_revision
    assert verify_revision(record)
    assert record["plan"]["knowledge_context"]["status"] == "disabled", "Synthetic demos must isolate private repository knowledge."
    assert not record["plan"]["knowledge_context"]["passages"]
    assert api(revision_url + "/files/knowledge.json") == record["plan"]["knowledge_context"]
    assert record["technical_accuracy"]["passed"] and record["technical_accuracy"]["complete"]
    assert api(revision_url + "/files/technical-context.json") == record["plan"]["technical_context"]
    assert api(revision_url + "/files/technical-accuracy.json") == record["technical_accuracy"]
    candidate = request(revision_url + "/files/candidate.txt")
    diff = request(revision_url + "/files/full.diff")
    trace = api(revision_url + "/files/trace.json")
    assert diff and trace
    assert hashlib.sha256(candidate).hexdigest() == result["candidate_sha256"]
    expect_conflict(revision_url + "/finalize", {"candidate_sha256": "stale", "plan_sha256": result["plan_sha256"]})
    acceptance = api(revision_url + "/finalize", {"candidate_sha256": result["candidate_sha256"], "plan_sha256": result["plan_sha256"]})
    final = request(acceptance["revision"])
    receipt = api(acceptance["receipt"])
    assert final == candidate and receipt["author_action"] == "accept_full_revision"
    from admissions_editor.review import fingerprint
    assert receipt["technical_context_sha256"] == fingerprint(record["plan"]["technical_context"])
    assert receipt["technical_accuracy_sha256"] == fingerprint(record["technical_accuracy"])
    assert request(source_url + "/files/review/run.json") == source_text
    assert json.loads(source_text)["snapshot"]["draft"] == demo["input"]["draft"]
    summary = {"url": base, "review_job_id": source_id, "revision_job_id": revision_id,
               "input_source": "repository_synthetic_revision_demo", "prediction_source": "fixtures_and_rule_adjudication",
               "sample_count": 1, "model_api_calls": 0, "status": result["status"], "stages": [s["stage"] for s in record["stages"]],
               "configured_revision_available": config.get("configured_revision_available"),
               "runtime_skills": skills, "validation_passed": True, "source_unchanged": True,
               "technical_accuracy_passed": True,
               "technical_claim_count": record["technical_accuracy"]["required_claim_count"],
               "glossary_entry_count": len(plan["glossary"]),
               "default_knowledge_status": knowledge_status["status"],
               "synthetic_demo_private_knowledge_disabled": True,
               "stale_plan_and_acceptance_rejected": True, "record_integrity_verified": True,
               "candidate_sha256": result["candidate_sha256"], "accepted_essay_url": acceptance["revision"],
               "acceptance_receipt_url": acceptance["receipt"], "storage": "Cloudflare D1",
               "background_execution": "Cloudflare Workflows"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "runtime_skills"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
