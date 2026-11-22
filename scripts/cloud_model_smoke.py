"""Review one built-in synthetic draft using real cloud-configured models.

This command makes billable OpenAI calls. Use --run ID to inspect/resume polling
an existing job without submitting another review. No manuscripts or secrets are
printed; full review artifacts stay in the authenticated cloud application.
"""

import argparse
import http.cookiejar
import json
import re
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--run", help="Existing cloud job ID; no new review is submitted.")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--out", type=Path, default=Path("runs/verification/cloud-model-e2e.json"))
    args = parser.parse_args()
    if args.run and not re.fullmatch(r"[a-f0-9]{32}", args.run):
        parser.error("Invalid job ID")
    base = args.url.rstrip("/")
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def api(path, body=None):
        headers = {"User-Agent": "AdmissionsEditorHealth/0.3", "Content-Type": "application/json", "Origin": base}
        request = urllib.request.Request(base + path, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        with client.open(request, timeout=30) as response:
            return json.load(response)

    api("/api/session", {"password": args.password_file.read_text().strip()})
    config = api("/api/config")
    if not config["configured_review_available"]:
        raise RuntimeError("Cloud model secrets are not configured.")
    job_id = args.run
    if not job_id:
        demo = api("/api/demo")
        job_id = api("/api/reviews", {"mode": "configured", "input": demo})["id"]
    summary = {"url": base, "job_id": job_id, "input_source": "existing_cloud_job" if args.run else "built_in_synthetic_draft", "sample_count": 1,
               "prediction_source": "real_openai_cloud", "transport_mocked": False, "new_review_submitted": not bool(args.run),
               "status": "pending", "configured_review_available": True}
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")

    save()
    print("Cloud model job: " + job_id, flush=True)
    deadline, previous = time.monotonic() + args.timeout, None
    while time.monotonic() < deadline:
        job = api("/api/reviews/" + job_id)
        if job["stage"] != previous:
            previous = job["stage"]
            print(job["stage"], flush=True)
        if job["state"] in {"complete", "partial", "failed"}:
            break
        time.sleep(2)
    else:
        print("Polling timed out; cloud task continues. Re-run with --run " + job_id, flush=True)
        return 2
    summary["status"] = job["state"]
    if job["state"] in {"complete", "partial"}:
        run = api(f"/api/reviews/{job_id}/files/review/run.json")
        from admissions_editor.workflow import verify_run
        verify_run(run)
        summary.update({"run_integrity_verified": True, "run_id": run["run_id"],
                        "reviewers": {role: {k: value[k] for k in ("status", "provider", "diagnostics", "metrics") if k in value}
                                      for role, value in run["reviewers"].items()},
                        "adjudication_status": run["adjudication"]["status"],
                        "adjudication_mode": run["adjudication"]["mode"],
                        "finding_group_count": len(run["groups"]),
                        "adjudicated_group_count": len(run["adjudication"]["decisions"]),
                        "adjudication_errors": run["adjudication"].get("validation_errors", []),
                        "adjudication_metrics": run["adjudication"].get("call_metrics", {}),
                        "eligible_change_count": len(run["changes"])})
        summary["recorded_model_api_attempts"] = (
            sum(v.get("metrics", {}).get("attempts", 0) for v in run["reviewers"].values()) +
            sum(v.get("attempts", 0) for v in run["adjudication"].get("call_metrics", {}).values()))
        assert all(v["provider"].startswith("openai:") for v in run["reviewers"].values())
        assert run["adjudication"]["mode"].startswith("openai:")
    save()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
