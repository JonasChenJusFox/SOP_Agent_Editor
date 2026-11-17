import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .providers import ROLE_NAMES, FixtureProvider, OpenAIProvider, review_schema
from .review import (
    RUN_VERSION,
    apply_changes,
    eligible_changes,
    overlapping_findings,
    read_evidence,
    render_report,
    run_review,
    sha256,
    unified_diff,
)


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def review_command(args: argparse.Namespace) -> int:
    if args.out.exists() or args.out.is_symlink():
        raise ValueError("Choose a new directory for the review output.")
    if args.provider == "openai":
        if args.fixtures or not args.model:
            raise ValueError("OpenAI review requires --model and cannot use --fixtures.")
        provider = OpenAIProvider(args.model)
    else:
        if not args.fixtures or args.model:
            raise ValueError("Fixture review requires --fixtures and cannot use --model.")
        provider = FixtureProvider(args.fixtures)

    draft_bytes = args.draft.read_bytes()
    requirements = args.requirements.read_bytes().decode("utf-8")
    preferences = args.preferences.read_bytes().decode("utf-8") if args.preferences else ""
    run = run_review(provider, draft_bytes, requirements, preferences, read_evidence(args.evidence_dir))
    original = draft_bytes.decode("utf-8")
    proposed = apply_changes(original, run["changes"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".review-", dir=args.out.parent) as staging:
        result = Path(staging) / "result"
        result.mkdir(mode=0o700)
        (result / "run.json").write_bytes(json_bytes(run))
        (result / "report.md").write_bytes(render_report(run).encode("utf-8"))
        (result / "proposed.diff").write_bytes(unified_diff(original, proposed).encode("utf-8"))

        # Claim the destination against another CLI run before publishing all artifacts.
        args.out.mkdir(mode=0o700)
        published = []
        try:
            # The approval command can open the run only after its companion files exist.
            for name in ("report.md", "proposed.diff", "run.json"):
                target = args.out / name
                (result / name).replace(target)
                published.append(target)
        except OSError:
            for target in published:
                target.unlink()
            args.out.rmdir()
            raise
    print(f"{run['status']}: {args.out / 'report.md'}")
    return 0 if run["status"] == "complete" else 2


def selected_changes(run: dict, draft_hash: str, requested: list[str]) -> list[dict]:
    if run.get("schema_version") != RUN_VERSION or run.get("status") != "complete":
        raise ValueError("Approval requires a complete review with a supported run format.")
    if run.get("draft_sha256") != draft_hash:
        raise ValueError("The draft differs from the reviewed version. Run the review again.")
    if len(set(requested)) != len(requested):
        raise ValueError("List each approved change once.")
    try:
        reviewers = run["reviewers"]
        if set(reviewers) != set(ROLE_NAMES):
            raise ValueError("The run is missing expected reviewers.")
        findings = []
        for role in ROLE_NAMES:
            result = reviewers[role]
            if result["status"] != "complete" or result["diagnostics"]:
                raise ValueError("All four reviewers must complete before approval.")
            for finding in result["findings"]:
                if finding["role"] != role:
                    raise ValueError("The review contains a mismatched finding role.")
                findings.append(finding)
        changes = eligible_changes(
            findings, overlapping_findings(findings), True, draft_hash, run["requirements_sha256"],
        )
        if changes != run["changes"]:
            raise ValueError("The recorded proposals differ from the eligible proposals.")
        by_id = {change["id"]: change for change in changes}
    except (AttributeError, KeyError, TypeError):
        raise ValueError("The review record is malformed.") from None
    if set(requested) - by_id.keys():
        raise ValueError("At least one selected change ID is unavailable for approval.")
    return [by_id[change_id] for change_id in requested]


def approve_command(args: argparse.Namespace) -> int:
    if args.out == args.draft or args.out.resolve() == args.draft.resolve():
        raise ValueError("Choose a separate path for the revised draft.")
    receipt = args.out.with_name(args.out.name + ".approval.json")
    if any(path.exists() or path.is_symlink() for path in (args.out, receipt)):
        raise ValueError("Choose a new output path for both the revision and its receipt.")

    run_bytes = (args.run / "run.json").read_bytes()
    run = json.loads(run_bytes)
    if not isinstance(run, dict):
        raise ValueError("The review record must be a JSON object.")
    original = args.draft.read_bytes()
    draft_hash = sha256(original)
    changes = selected_changes(run, draft_hash, args.change)
    revised = apply_changes(original.decode("utf-8"), changes).encode("utf-8")
    approval = json_bytes({
        "schema_version": RUN_VERSION,
        "approved_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_sha256": sha256(run_bytes),
        "draft_sha256": draft_hash,
        "revised_sha256": sha256(revised),
        "approved_change_ids": args.change,
    })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".approval-", dir=args.out.parent) as staging:
        staged_revision = Path(staging) / "revision"
        staged_receipt = Path(staging) / "receipt"
        staged_revision.write_bytes(revised)
        staged_receipt.write_bytes(approval)
        staged_revision.chmod(0o600)
        staged_receipt.chmod(0o600)
        os.link(staged_receipt, receipt)
        try:
            os.link(staged_revision, args.out)
        except OSError:
            receipt.unlink()
            raise

    print(f"Approved {len(changes)} change(s): {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and approve changes to an admissions essay.")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="Run the independent editorial reviewers.")
    review.add_argument("--draft", type=Path, required=True)
    review.add_argument("--requirements", type=Path, required=True)
    review.add_argument("--preferences", type=Path)
    review.add_argument("--evidence-dir", type=Path, help="Directory of .txt or .md evidence files.")
    review.add_argument("--provider", choices=("fixture", "openai"), default="fixture")
    review.add_argument("--fixtures", type=Path, help="Directory of prewritten reviewer JSON files.")
    review.add_argument("--model", help="OpenAI model supporting Structured Outputs.")
    review.add_argument("--out", type=Path, required=True, help="New review output directory.")

    approve = commands.add_parser("approve", help="Apply specifically approved changes to a new file.")
    approve.add_argument("--run", type=Path, required=True, help="Review output directory.")
    approve.add_argument("--draft", type=Path, required=True)
    approve.add_argument("--change", action="append", required=True, help="Change ID, repeatable.")
    approve.add_argument("--out", type=Path, required=True, help="New revised draft file.")
    commands.add_parser("schema", help="Print the reviewer output JSON Schema.")

    args = parser.parse_args(argv)
    try:
        if args.command == "review":
            return review_command(args)
        if args.command == "approve":
            return approve_command(args)
        print(json.dumps(review_schema(), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
