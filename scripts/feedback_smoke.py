"""Exercise the one-comment workflow and two Word downloads using synthetic input."""

import argparse
import hashlib
import http.cookiejar
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Choose a new output directory.")
    args.out.mkdir(parents=True)
    base = args.url.rstrip("/")
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        headers = {"Origin": base, "User-Agent": "AdmissionsEditorFeedbackQA/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        with client.open(req, timeout=45) as response:
            return response.read(), response.headers

    def api(path, body=None):
        return json.loads(request(path, body)[0])

    def wait(path):
        until = time.monotonic() + 150
        while time.monotonic() < until:
            job = api(path)
            if job["state"] in {"complete", "partial", "failed"}:
                return job
            time.sleep(1)
        raise TimeoutError("Synthetic feedback workflow timed out.")

    def reject_stale(path, body):
        try:
            api(path, body)
        except urllib.error.HTTPError as error:
            assert error.code == 409, error.code
            return
        raise AssertionError("A stale feedback request was accepted.")

    if args.password_file:
        api("/api/session", {"password": args.password_file.read_text().strip()})
    demo = api("/api/revision-demo")
    review_id = api("/api/reviews", {"mode": "demo", "input": demo["input"]})["id"]
    review_url = "/api/reviews/" + review_id
    review = wait(review_url)
    assert review["state"] == "complete", review.get("stage")
    report = review["result"]["editorial_report"]
    assert report["issues"] and [r["number"] for r in report["issues"]] == list(range(1, len(report["issues"]) + 1))
    source_before = request(review_url + "/files/review/run.json")[0]
    body = {"feedback": demo["feedback"], "run_sha256": review["result"]["run_sha256"],
            "editorial_report_sha256": report["report_sha256"],
            **{key: value for key, value in demo["plan_input"].items() if key in
               {"confirmed_facts", "essay_type", "word_limit", "writing_sample", "glossary"}}}
    endpoint = review_url + "/feedback-revisions"
    reject_stale(endpoint, {**body, "editorial_report_sha256": "0" * 64})
    revision_id = api(endpoint, body)["id"]
    revision_url = "/api/revisions/" + revision_id
    revision = wait(revision_url)
    assert revision["state"] == "complete", revision.get("stage")
    result = revision["result"]
    assert result["status"] == "ready" and result["validation"]["passed"], result.get("blockers")
    assert result["feedback"] == demo["feedback"]
    assert result["feedback_analysis"]["decisions"] and result["selected_skill_ids"]
    assert [s["stage"] for s in result["stages"]] == ["sop_revise", "polish", "humanize", "verify_revision"]
    downloads = {}
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for filename, output in (("essay.docx", "optimized-essay.docx"), ("change-report.docx", "change-report.docx")):
        data, headers = request(revision_url + "/documents/" + filename)
        assert headers["Content-Type"].split(";")[0] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert "attachment" in headers.get("Content-Disposition", "")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.testzip() is None
            document = ET.fromstring(archive.read("word/document.xml"))
            paragraphs = ["".join(node.text or "" if node.tag == namespace + "t" else "\n" if node.tag == namespace + "br" else ""
                                  for node in p.iter()) for p in document.findall(".//" + namespace + "body/" + namespace + "p")]
            if filename == "essay.docx":
                assert paragraphs == result["candidate_text"].strip().split("\n\n")
            else:
                assert "实际段落变动" in paragraphs and demo["feedback"] in "\n".join(paragraphs)
        (args.out / output).write_bytes(data)
        downloads[output] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert request(review_url + "/files/review/run.json")[0] == source_before
    # Candidate downloads intentionally do not create an author-acceptance receipt.
    summary = {"url": base, "review_job_id": review_id, "revision_job_id": revision_id,
        "input_source": "repository_synthetic_revision_demo", "prediction_source": "offline_fixture_and_rule",
        "sample_count": 1, "model_api_calls": 0, "status": result["status"],
        "numbered_issue_count": len(report["issues"]), "single_feedback_preserved": True,
        "selected_skill_ids": result["selected_skill_ids"], "four_revision_stages_complete": True,
        "technical_accuracy_passed": result["technical_accuracy"]["passed"], "stale_report_rejected": True,
        "source_review_unchanged": True, "docx_downloads": downloads}
    (args.out / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
