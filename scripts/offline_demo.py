"""Synthetic-only CLI acceptance run. All commands and exit codes are recorded."""

import argparse
import json
import platform
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/offline-demo"))
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    commands = []

    def call(*arguments, expected=0):
        if arguments[0] in {"review", "approve", "retry-adjudication", "revision-plan", "revise", "finalize-revision"}:
            arguments = (*arguments, "--no-knowledge")
        command = [sys.executable, "-m", "admissions_editor", *map(str, arguments)]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=60)
        commands.append({"command": shlex.join(command), "expected_exit": expected, "actual_exit": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        print(f"[{result.returncode}] {shlex.join(command)}")
        if result.returncode != expected:
            raise RuntimeError(result.stdout + result.stderr)

    common = ["--requirements", "examples/requirements.txt", "--preferences", "examples/preferences.txt",
              "--evidence-dir", "examples/evidence", "--claims", "examples/claims.json",
              "--sources", "examples/sources.json", "--as-of", "2026-11-18"]
    call("review", "--config", "examples/offline.toml", "--draft", "examples/draft.txt", *common, "--out", out / "round1")
    first = json.loads((out / "round1/run.json").read_text())
    if len(first["changes"]) != 1:
        raise RuntimeError("The synthetic demo expects exactly one article correction.")
    # This automatic selection is confined to the repository's synthetic sample.
    call("approve", "--run", out / "round1", "--draft", "examples/draft.txt", "--change", first["changes"][0]["id"], "--out", out / "revised.txt")
    if (out / "revised.txt").read_text() != (ROOT / "examples/draft.txt").read_text().replace("the the replay tool", "the replay tool"):
        raise RuntimeError("Approved output does not match the selected correction.")
    fixtures = out / "round2-fixtures"
    shutil.copytree(ROOT / "examples/fixtures", fixtures)
    (fixtures / "language.json").write_text(json.dumps({"role": "language", "findings": []}) + "\n")
    call("review", "--fixtures", fixtures, "--draft", out / "revised.txt", *common,
         "--parent-run", out / "round1", "--approval-receipt", out / "revised.txt.approval.json", "--out", out / "round2")
    second = json.loads((out / "round2/run.json").read_text())
    if not any(i["status"] == "resolved" and i["category"] == "language" for i in second["issues"]):
        raise RuntimeError("The approved local correction was not tracked as resolved.")
    call("generate", "--materials", "examples/materials.json", "--requirements", "examples/requirements.txt", "--out", out / "writer")
    call("evaluate", "--dataset", "examples/evaluation/dataset.json", "--predictions", "examples/evaluation/predictions.json", "--out", out / "evaluation")
    call("benchmark", "--dataset", "examples/evaluation/dataset.json", "--fixtures", "examples/evaluation/fixtures",
         "--strategy", "four", "--out", out / "benchmark", "--bootstrap", 100)
    call("annotation-template", "--dataset", "examples/evaluation/dataset.json", "--annotator", "human-a", "--out", out / "annotation-a")
    # Required failure paths are exercised through the actual CLI as well as unit tests.
    stale = out / "stale.txt"
    stale.write_text((ROOT / "examples/draft.txt").read_text() + "\nSynthetic later edit.\n")
    call("approve", "--run", out / "round1", "--draft", stale, "--change", first["changes"][0]["id"], "--out", out / "must-not-exist.txt", expected=1)
    if (out / "must-not-exist.txt").exists():
        raise RuntimeError("Stale approval unexpectedly wrote a draft.")
    missing = out / "missing-role-fixtures"
    shutil.copytree(ROOT / "examples/fixtures", missing)
    (missing / "skeptical.json").unlink()
    call("review", "--fixtures", missing, "--draft", "examples/draft.txt", *common, "--out", out / "partial", expected=2)
    call("approve", "--run", out / "partial", "--draft", "examples/draft.txt", "--change", first["changes"][0]["id"], "--out", out / "partial-must-not-exist.txt", expected=1)
    record = {"python": platform.python_version(), "platform": platform.platform(), "jsonschema": version("jsonschema"),
              "source": "synthetic_offline_only", "real_api_calls": 0, "commands": commands,
              "approved_change_id": first["changes"][0]["id"], "status": "passed"}
    (out / "validation.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(f"Offline acceptance passed: {out / 'validation.json'}")


if __name__ == "__main__":
    main()
