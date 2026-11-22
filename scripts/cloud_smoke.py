"""HTTPS smoke test using only the built-in synthetic demo; no model calls."""

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
    parser.add_argument("--out", type=Path, default=Path("runs/verification/cloud-native-e2e.json"))
    args = parser.parse_args()
    base = args.url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(path, body=None):
        headers = {"User-Agent": "AdmissionsEditorHealth/0.3", "Accept": "application/json", "Origin": base}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with client.open(req, timeout=30) as response:
            return response.status, response.read()

    def api(path, body=None):
        _, data = request(path, body)
        return json.loads(data)

    page_status, page = request("/")
    assert b"Attach" in page
    initial = api("/api/session")
    assert initial["password_required"] and initial["deployment"] == "cloudflare-native"
    try:
        api("/api/config")
        raise AssertionError("Unauthenticated API was accessible")
    except urllib.error.HTTPError as error:
        assert error.code == 401
    api("/api/session", {"password": args.password_file.read_text().strip()})
    config = api("/api/config")
    demo = api("/api/demo")
    job = api("/api/reviews", {"mode": "demo", "input": demo})
    job_id = job["id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = api("/api/reviews/" + job_id)
        if result["state"] in {"complete", "partial", "failed"}:
            break
        time.sleep(1)
    assert result["state"] == "complete", result
    record = result["result"]
    approved = api(f"/api/reviews/{job_id}/approve", {"run_sha256": record["run_sha256"], "draft_sha256": record["draft_sha256"],
                                                        "change_ids": [record["changes"][0]["id"]]})
    _, revision = request(approved["revision"])
    receipt = api(approved["receipt"])
    assert receipt["revised_sha256"] == hashlib.sha256(revision).hexdigest()
    run = api(f"/api/reviews/{job_id}/files/review/run.json")
    from admissions_editor.workflow import verify_run, validated_changes
    verify_run(run)
    assert validated_changes(run)
    assert all(v["provider"] == "fixture" for v in run["reviewers"].values())
    assert run["adjudication"]["mode"].startswith("rule:")
    summary = {"url": base, "job_id": job_id, "deployment": "cloudflare-native", "requires_local_backend": False,
               "page_http_status": page_status, "password_login": "passed", "unauthenticated_api_status": 401,
               "configured_review_available": config["configured_review_available"], "demo_job_status": result["state"],
               "approved_change_count": 1, "revision_hash_verified": True, "run_integrity_verified": True,
               "model_api_calls": 0, "storage": "Cloudflare D1", "background_execution": "Cloudflare Workflows"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
