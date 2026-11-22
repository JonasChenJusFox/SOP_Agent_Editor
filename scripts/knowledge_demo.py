"""Offline default-repository retrieval, revision and stale-source acceptance demo.

All repositories and applicant claims created here are synthetic. The script
does not touch the populated private/knowledge sources of the editor project.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from admissions_editor.revision_schema import CHECK_NAMES

ROOT = Path(__file__).resolve().parents[1]
DRAFT = (
    "For NearBite, I used FAISS embeddings for retrieval and built the search interface. "
    "My teammates developed the ranking model.\n\n"
    "For AitherPath, I implemented an evaluation dashboard that displayed precision and recall from the validation set.\n"
)
FINAL = DRAFT.replace("For NearBite,", "In NearBite,").replace("For AitherPath,", "In AitherPath,")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/knowledge-delivery"))
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to((ROOT / "runs").resolve()) or out == (ROOT / "runs").resolve() or out.exists():
        parser.error("Choose a new output directory inside this project's runs/ directory.")
    out.mkdir(parents=True, mode=0o700)
    state = {"input_source": "synthetic_demo_repositories", "prediction_source": "fixture",
             "model_api_calls": 0, "status": "running", "commands": []}

    def command(argv, cwd=out, expected=0):
        result = subprocess.run([str(value) for value in argv], cwd=cwd, capture_output=True, text=True,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
        state["commands"].append({"command": [str(value) for value in argv], "cwd": str(cwd),
                                  "expected_exit": expected, "actual_exit": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr})
        save_json(out / "validation.json", state)
        if result.returncode != expected:
            raise RuntimeError(f"Unexpected exit {result.returncode}; inspect {out / 'validation.json'}")
        return result.stdout

    def cli(*argv, expected=0):
        return command([sys.executable, "-m", "admissions_editor", *argv], expected=expected)

    def git(repo, *argv):
        return command(["git", "-C", repo, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                        "-c", "core.fsmonitor=false", *argv], cwd=repo)

    def write(repo, relative, value):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def source_hashes(repos):
        return {f"{repo.name}/{path.relative_to(repo).as_posix()}": digest(path.read_bytes())
                for repo in repos for path in repo.rglob("*") if path.is_file() and ".git" not in path.relative_to(repo).parts}

    (out / "pyproject.toml").write_text('[project]\nname = "admissions-editor-agent"\nversion = "0.0.0"\n')
    (out / "draft.txt").write_text(DRAFT)
    (out / "requirements.txt").write_text("Describe your actual technical contributions to both projects. Maximum 500 English words.\n")
    (out / "preferences.txt").write_text("Preserve methods, evaluation conditions and individual versus team contributions.\n")
    repos_root = out / "private/knowledge/repos"
    nearbite, aitherpath = repos_root / "nearbite", repos_root / "aitherpath"
    for repo, paragraph in zip((nearbite, aitherpath), DRAFT.strip().split("\n\n")):
        repo.mkdir(parents=True)
        write(repo, "README.md", "# Synthetic repository fixture\n\nThis is an invented project for offline behavior testing.\n\n" + paragraph + "\n")
    write(nearbite, "src/search.py", "# Synthetic NearBite FAISS embeddings retrieval and search interface.\n"
          "def retrieve_candidates(faiss_index, embeddings):\n    return faiss_index.search(embeddings, 10)\n\n"
          "def search_interface(candidates):\n    return {'candidates': candidates}\n")
    write(nearbite, "tests/test_search.py", "# Synthetic NearBite retrieval test; this is not a production outcome.\n"
          "def test_retrieval_interface():\n    assert {'candidates': []}['candidates'] == []\n")
    write(nearbite, "evaluation/metrics.json", json.dumps({"project": "NearBite", "source": "synthetic_fixture",
          "metric": "MRR", "value": 0.42, "split": "validation"}, indent=2) + "\n")
    write(aitherpath, "src/dashboard.py", "# Synthetic AitherPath evaluation dashboard for validation precision and recall.\n"
          "def evaluation_dashboard(precision, recall):\n    return {'precision': precision, 'recall': recall}\n")
    write(aitherpath, "tests/test_dashboard.py", "# Synthetic AitherPath validation metrics display test.\n"
          "def test_precision_recall_dashboard():\n    assert {'precision': 0.8, 'recall': 0.7}['precision'] == 0.8\n")
    write(aitherpath, "evaluation/metrics.json", json.dumps({"project": "AitherPath", "source": "synthetic_fixture",
          "precision": 0.8, "recall": 0.7, "split": "validation"}, indent=2) + "\n")
    for repo in (nearbite, aitherpath):
        git(repo, "init", "--quiet", "--template=")
        git(repo, "add", "README.md", "src", "tests", "evaluation")
        git(repo, "-c", "user.name=Synthetic Demo", "-c", "user.email=synthetic@example.invalid",
            "commit", "--quiet", "-m", "Synthetic knowledge demonstration fixture")
    initial_source_hashes = source_hashes((nearbite, aitherpath))
    reviewer_fixtures = out / "reviewer-fixtures"
    for role in ("structure", "language", "admissions", "skeptical"):
        save_json(reviewer_fixtures / f"{role}.json", {"role": role, "findings": []})

    cli("knowledge", "refresh", "--out", out / "index-initial")
    cli("knowledge", "status", "--out", out / "index-status")
    cli("knowledge", "query", "--draft", out / "draft.txt", "--out", out / "draft-query")
    cli("knowledge", "query", "--claim", "NearBite evaluation MRR on validation data", "--out", out / "evaluation-query")
    query = json.loads((out / "draft-query/knowledge.json").read_text())
    assert {p["repository"] for p in query["passages"]} == {"nearbite", "aitherpath"}
    evaluation = json.loads((out / "evaluation-query/knowledge.json").read_text())
    assert any(p["repository"] == "nearbite" and p["kind"] == "evaluation" for p in evaluation["passages"])
    for passage in query["passages"]:
        source = repos_root / passage["repository"] / passage["file_path"]
        excerpt = "\n".join(source.read_text().splitlines()[passage["line_start"] - 1:passage["line_end"]])
        assert excerpt.rstrip("\n") == passage["excerpt"].rstrip("\n")

    cli("review", "--draft", out / "draft.txt", "--requirements", out / "requirements.txt",
        "--preferences", out / "preferences.txt", "--fixtures", reviewer_fixtures, "--out", out / "review")
    run = json.loads((out / "review/run.json").read_text())
    assert run["status"] == "complete"
    assert {p["repository"] for p in run["snapshot"]["knowledge_context"]["passages"]} == {"nearbite", "aitherpath"}
    assert "Repository knowledge" in (out / "review/report.md").read_text()
    assert "README.md:L" in (out / "review/report.md").read_text()
    cli("revision-plan", "--run", out / "review", "--draft", out / "draft.txt", "--out", out / "plan")
    plan = json.loads((out / "plan/plan.json").read_text())
    fixtures = out / "revision-fixtures"
    for stage, essay in (("sop_revise", DRAFT), ("polish", FINAL), ("humanize", FINAL)):
        save_json(fixtures / f"{stage}.json", {"essay": essay, "addressed_finding_ids": [], "used_fact_ids": [],
                                             "unresolved_questions": [], "technical_flags": []})
    passages = plan["knowledge_context"]["passages"]
    technical_claims = {claim["id"]: claim for claim in plan["technical_context"]["claims"]}
    original_paragraphs = run["snapshot"]["paragraphs"]
    final_paragraphs = FINAL.strip().split("\n\n")
    claim_changes, technical_checks = [], []
    for index, paragraph in enumerate(original_paragraphs):
        passage = next((p for p in passages if paragraph["text"] in p["excerpt"]), None)
        claim_changes.append({"original_quote": paragraph["text"], "final_quote": final_paragraphs[index],
            "source_type": "evidence" if passage else "original", "source_id": passage["id"] if passage else paragraph["id"],
            "source_quote": paragraph["text"], "status": "supported"})
    for claim_id in plan["technical_context"]["check_required_claim_ids"]:
        claim = technical_claims[claim_id]
        passage = next((p for p in passages if claim["quote"] in p["excerpt"]), None)
        quote = claim["quote"] if claim["quote"] in FINAL else final_paragraphs[int(claim["source_id"][1:]) - 1]
        technical_checks.append({"claim_id": claim_id, "status": "preserved" if claim["quote"] in FINAL else "supported_change",
            "original_quote": claim["quote"], "final_quote": quote, "source_type": "evidence" if passage else "draft",
            "source_id": passage["id"] if passage else claim["source_id"], "source_quote": claim["quote"],
            "reason": "Synthetic exact-source audit: style changes preserve methods, conditions and recorded applicant/team scope."})
    save_json(fixtures / "verify_revision.json", {"checks": [{"check": name, "passed": True,
        "rationale": "Synthetic source-bound behavior test, not a real model quality judgment."} for name in CHECK_NAMES],
        "finding_checks": [], "claim_changes": claim_changes, "technical_checks": technical_checks,
        "unresolved_questions": []})
    assert any(check["source_id"].startswith("KB-") for check in technical_checks)
    cli("revise", "--run", out / "review", "--draft", out / "draft.txt", "--plan", out / "plan",
        "--fixtures", fixtures, "--out", out / "revision")
    record = json.loads((out / "revision/revision.json").read_text())
    assert record["status"] == "ready" and record["technical_accuracy"]["passed"]
    cli("finalize-revision", "--run", out / "review", "--draft", out / "draft.txt", "--revision", out / "revision",
        "--candidate", out / "revision/candidate.txt", "--out", out / "final.txt")
    assert (out / "final.txt").read_text() == FINAL and (out / "draft.txt").read_text() == DRAFT
    assert source_hashes((nearbite, aitherpath)) == initial_source_hashes

    changed_source = nearbite / "src/search.py"
    changed_source.write_text(changed_source.read_text() + "\n# Synthetic authorized source change after planning.\n")
    cli("revise", "--run", out / "review", "--draft", out / "draft.txt", "--plan", out / "plan",
        "--fixtures", fixtures, "--out", out / "must-not-revise", expected=1)
    cli("finalize-revision", "--run", out / "review", "--draft", out / "draft.txt", "--revision", out / "revision",
        "--candidate", out / "revision/candidate.txt", "--out", out / "must-not-finalize.txt", expected=1)
    assert not (out / "must-not-revise").exists() and not (out / "must-not-finalize.txt").exists()
    cli("knowledge", "refresh", "--out", out / "index-refreshed")
    cli("knowledge", "query", "--draft", out / "draft.txt", "--out", out / "query-refreshed")
    refreshed = json.loads((out / "query-refreshed/knowledge.json").read_text())
    before = {source["name"]: source["snapshot_hash"] for source in query["sources"]}
    after = {source["name"]: source["snapshot_hash"] for source in refreshed["sources"]}
    assert before["nearbite"] != after["nearbite"] and before["aitherpath"] == after["aitherpath"]
    assert len([key for key, value in source_hashes((nearbite, aitherpath)).items() if initial_source_hashes.get(key) != value]) == 1
    state.update(status="passed", sample_count=1, source_repositories="synthetic_only",
        default_sources_without_repo_flags=True, repository_sources_read_only_during_editorial_commands=True,
        source_repository_changed=True,
        source_change_scope="one_explicit_synthetic_fixture_change_after_acceptance",
        editor_git_commits_created=0,
        source_snapshot_refreshed=True, stale_revision_rejected=True, stale_acceptance_rejected=True,
        draft_unchanged=True, knowledge_citations_verified=True, technical_repository_evidence_used=True,
        retrieved_passage_count=len(query["passages"]), index_path=str(out / "private/knowledge/index.sqlite3"),
        final_essay=str(out / "final.txt"), review_report=str(out / "review/report.md"),
        knowledge_report=str(out / "revision/knowledge.json"), technical_report=str(out / "revision/technical-accuracy.json"),
        quality_scope="Offline behavior validation using invented repositories and fixture judgments; no real model or human quality metric.")
    save_json(out / "validation.json", state)
    print(f"Knowledge default-source demonstration passed: {out / 'validation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
