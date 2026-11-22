"""Offline full-revision acceptance demonstration with fictional fixtures only."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/full-revision-demo"))
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        parser.error("Choose a new output directory.")
    out.mkdir(parents=True, mode=0o700)
    draft = ROOT / "examples/draft.txt"
    original_sha = hashlib.sha256(draft.read_bytes()).hexdigest()
    commands = []

    def run(*values, expected=0):
        if values[0] in {"review", "revision-plan", "revise", "finalize-revision"}:
            values = (*values, "--no-knowledge")
        command = [sys.executable, "-m", "admissions_editor", *map(str, values)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        commands.append({"command": command, "expected_exit": expected, "actual_exit": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != expected:
            raise RuntimeError(f"Unexpected exit {result.returncode} for {values[0]}: {result.stderr}")

    run("review", "--draft", draft, "--requirements", ROOT / "examples/requirements.txt",
        "--preferences", ROOT / "examples/preferences.txt", "--fixtures", ROOT / "examples/fixtures",
        "--evidence-dir", ROOT / "examples/revision/evidence", "--claims", ROOT / "examples/claims.json",
        "--sources", ROOT / "examples/revision/sources.json", "--as-of", "2026-11-18", "--out", out / "review")
    common = ["--run", out / "review", "--draft", draft]
    run("revision-plan", *common, "--out", out / "plan-template")
    run("revision-plan", *common, "--decisions", ROOT / "examples/revision/decisions.json",
        "--facts", ROOT / "examples/revision/facts.json", "--answers", ROOT / "examples/revision/answers.json",
        "--out", out / "plan")
    run("revise", *common, "--plan", out / "plan", "--fixtures", ROOT / "examples/revision/fixtures", "--out", out / "revision")
    run("finalize-revision", *common, "--revision", out / "revision", "--candidate", out / "revision/candidate.txt", "--out", out / "final.txt")
    stale = out / "stale-draft.txt"
    stale.write_bytes(draft.read_bytes() + b"\nAuthor changed the source.\n")
    run("finalize-revision", "--run", out / "review", "--draft", stale, "--revision", out / "revision",
        "--candidate", out / "revision/candidate.txt", "--out", out / "must-not-exist.txt", expected=1)
    run("revision-plan", *common, "--decisions", ROOT / "examples/revision/decisions.json",
        "--facts", ROOT / "examples/revision/facts.json", "--out", out / "plan-missing-answer")
    run("revise", *common, "--plan", out / "plan-missing-answer", "--fixtures", ROOT / "examples/revision/fixtures",
        "--out", out / "blocked", expected=2)
    run("finalize-revision", *common, "--revision", out / "blocked", "--candidate", out / "blocked/candidate.txt",
        "--out", out / "must-not-finalize.txt", expected=1)
    assert hashlib.sha256(draft.read_bytes()).hexdigest() == original_sha
    assert not (out / "must-not-exist.txt").exists() and not (out / "must-not-finalize.txt").exists()
    assert not (out / "blocked/final-essay.txt").exists()
    record = json.loads((out / "revision/revision.json").read_text())
    summary = {"input_source": "fictional_fixture", "real_model_api_calls": 0, "source_unchanged": True,
               "technical_accuracy_passed": record["technical_accuracy"]["passed"],
               "technical_context": str(out / "revision/technical-context.json"),
               "technical_report": str(out / "revision/technical-accuracy.json"),
               "revision_status": record["status"], "final_word_count": len((out / "final.txt").read_text().split()),
               "final_essay": str(out / "final.txt"), "full_diff": str(out / "revision/full.diff"),
               "finding_trace": str(out / "revision/trace.json"), "verification": str(out / "revision/validation.json"),
               "acceptance_receipt": str(out / "final.txt.revision-acceptance.json"), "commands": commands}
    (out / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"Full revision offline demonstration passed: {out / 'validation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
