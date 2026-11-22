import argparse
import json
import os
import sys
import copy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .providers import ROLE_NAMES, ReviewerError, review_schema
from .config import load_config, review_provider, task_provider
from .contracts import SCHEMAS
from .generation import generate_draft
from .evaluation import DATASET, PREDICTIONS, evaluate, render_evaluation
from .annotations import ANNOTATIONS, agreement, annotation_pack
from .workflow import (WORKFLOW_PROMPT_VERSION, enrich_run, render_workflow, retry_adjudication,
                       validated_changes, verify_run)
from .review import (
    RUN_VERSION,
    apply_changes,
    paragraph_spans,
    read_evidence,
    render_report,
    run_review,
    sha256,
    fingerprint,
    unified_diff,
)


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path else default


def knowledge_options(args, config=None):
    return {"config": config if config is not None else load_config(getattr(args, "config", None)),
            "repos": getattr(args, "repo", None), "knowledge_config": getattr(args, "knowledge_config", None),
            "disabled": getattr(args, "no_knowledge", False)}


def knowledge_notice(context):
    for diagnostic in (context or {}).get("diagnostics", []):
        print("Knowledge: " + diagnostic, file=sys.stderr)


def knowledge_command(args):
    from .knowledge import KnowledgeIndex
    from .knowledge_sources import essay_claims, settings, unavailable_knowledge_context
    selected = settings(**knowledge_options(args))
    if not selected["enabled"]:
        result = unavailable_knowledge_context("Repository knowledge was explicitly disabled.", disabled=True)
    else:
        index = KnowledgeIndex(Path(selected["index"]), selected["sources"],
                               auto_refresh=selected["auto_refresh"] if args.knowledge_action == "query" else False)
        if args.knowledge_action == "refresh":
            result = index.refresh()
        elif args.knowledge_action == "status":
            result = index.retrieve([])
        else:
            claims = essay_claims(args.draft.read_text(), [source["name"] for source in selected["sources"]]) if args.draft else [{"id": "QUERY", "text": args.claim}]
            result = index.retrieve(claims, limit_per_claim=selected["limit_per_claim"])
    result = {**result, "index_path": selected["index"]}
    if result.get("schema_version") == "knowledge-context-1":
        result["snapshot_sha256"] = fingerprint({key: value for key, value in result.items() if key != "snapshot_sha256"})
    if args.out:
        publish_directory(args.out, {"knowledge.json": json_bytes(result)})
        print(f"Knowledge: {args.out / 'knowledge.json'}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def publish_directory(destination, artifacts):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new output directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".editor-", dir=destination.parent) as staging:
        result = Path(staging) / "result"
        result.mkdir(mode=0o700)
        for name, content in artifacts.items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Artifact names must stay within the output directory.")
            path = result / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
        # mkdir claims the name without overwriting an existing run, even if empty.
        destination.mkdir(mode=0o700)
        try:
            for path in sorted(result.iterdir(), key=lambda p: (p.name == "run.json", p.name)):
                path.replace(destination / path.name)
        except OSError:
            import shutil
            shutil.rmtree(destination)
            raise


def review_command(args: argparse.Namespace) -> int:
    if args.out.exists() or args.out.is_symlink():
        raise ValueError("Choose a new directory for the review output.")
    config = load_config(args.config)
    provider = review_provider(args, config)
    workflow = task_provider(config)

    draft_bytes = args.draft.read_bytes()
    requirements = args.requirements.read_bytes().decode("utf-8")
    preferences = args.preferences.read_bytes().decode("utf-8") if args.preferences else ""
    parent, parent_sha, approved_ids = None, None, []
    if bool(args.parent_run) != bool(args.approval_receipt):
        raise ValueError("A follow-up review needs both --parent-run and --approval-receipt.")
    if args.parent_run:
        parent_bytes = (args.parent_run / "run.json").read_bytes()
        parent = json.loads(parent_bytes)
        verify_run(parent)
        parent_sha = sha256(parent_bytes)
        receipt = read_json(args.approval_receipt)
        approved_ids = receipt["approved_change_ids"]
        changes = selected_changes(parent, parent["draft_sha256"], approved_ids)
        expected_revision = apply_changes(parent["snapshot"]["draft"], changes).encode()
        if receipt["run_sha256"] != parent_sha or receipt["draft_sha256"] != parent["draft_sha256"] or \
                receipt["input_sha256"] != parent["input_sha256"] or \
                receipt["revised_sha256"] != sha256(draft_bytes) or expected_revision != draft_bytes:
            raise ValueError("Follow-up draft/approval does not match the parent run.")
    feedback = read_json(args.feedback) if args.feedback else None
    if feedback:
        if not parent or feedback.get("run_sha256") != parent_sha or feedback.get("input_sha256") != parent["input_sha256"]:
            raise ValueError("Author feedback does not match the parent review.")
        parent = copy.deepcopy(parent)
        by_id = {i["issue_id"]: i for i in parent["issues"]}
        if len({d["issue_id"] for d in feedback["decisions"]}) != len(feedback["decisions"]):
            raise ValueError("Duplicate feedback decisions.")
        for decision in feedback["decisions"]:
            if decision["issue_id"] not in by_id or decision["decision"] not in {"resolved", "deferred", "rejected"} or not decision["reason"].strip():
                raise ValueError("Invalid author feedback.")
            issue = by_id[decision["issue_id"]]
            issue["status"] = "resolved" if decision["decision"] == "resolved" else "deferred"
            issue["author_decision"] = decision
    manifest = read_json(args.claims, {"claims": []})
    source_metadata = read_json(args.sources, {})
    max_rounds = args.max_rounds if args.max_rounds is not None else (
        parent["round"]["max_rounds"] if parent else config.get("rounds", {}).get("max_rounds", 3))
    if not 1 <= max_rounds <= 20 or (parent and parent["round"]["number"] + 1 > max_rounds):
        raise ValueError("Review round limit reached or invalid; explicitly set --max-rounds between 1 and 20.")
    as_of = args.as_of or datetime.now().astimezone().date().isoformat()
    evidence = read_evidence(args.evidence_dir)
    from .knowledge_sources import prepare_knowledge
    from .review import knowledge_evidence
    knowledge = prepare_knowledge(draft_bytes.decode("utf-8"), **knowledge_options(args, config))
    knowledge_notice(knowledge)
    expanded_evidence = {**evidence, **knowledge_evidence(knowledge)}
    from .facts import build_ledger
    # Reject malformed evidence mappings before any paid model call.
    build_ledger({"draft": draft_bytes.decode("utf-8"), "paragraphs": [
        {"id": k, "text": v[1]} for k, v in paragraph_spans(draft_bytes.decode("utf-8")).items()],
        "evidence": expanded_evidence}, manifest, source_metadata, as_of)
    context = {"claims": manifest, "source_metadata": source_metadata, "as_of": as_of,
               "review_configuration": {role: provider.label_for(role) for role in ROLE_NAMES},
               "workflow_provider": workflow.label, "parent_run_sha256": parent_sha,
               "workflow_prompt_version": WORKFLOW_PROMPT_VERSION,
               "author_feedback": feedback,
               "max_rounds": max_rounds, "author_ready": args.author_ready, "reverse_opinions": args.reverse_opinions}
    run = run_review(provider, draft_bytes, requirements, preferences, evidence,
                     max_workers=config.get("review", {}).get("max_workers", 4), context=context,
                     knowledge_context=knowledge)
    run = enrich_run(run, workflow, manifest=manifest, source_metadata=source_metadata, as_of=as_of,
                     parent=parent, parent_sha=parent_sha, approved_ids=approved_ids, max_rounds=max_rounds,
                     author_ready=args.author_ready, reverse=args.reverse_opinions)
    return write_review(args.out, run)


def review_artifacts(run):
    original = run["snapshot"]["draft"]
    proposed = apply_changes(original, run["changes"])

    artifacts = {"report.md": (render_report(run) + render_workflow(run)).encode(),
                 "proposed.diff": unified_diff(original, proposed).encode(),
                 "snapshot.json": json_bytes(run["snapshot"]), "ledger.json": json_bytes(run["ledger"]),
                 "adjudication.json": json_bytes(run["adjudication"]),
                 "issues.json": json_bytes({"round": run["round"], "issues": run["issues"]}),
                 "audit.json": json_bytes({"events": run["audit"]}),
                 "candidate-details.json": json_bytes({"items": run["candidate_details"]})}
    if run["snapshot"].get("knowledge_context") is not None:
        artifacts["knowledge.json"] = json_bytes(run["snapshot"]["knowledge_context"])
    for item in run["candidate_details"]:
        artifacts[f"changes/{item['finding_id']}.diff"] = item["diff"].encode()
    # Publish run.json last; a visible companion file never authorizes approval by itself.
    artifacts["run.json"] = json_bytes(run)
    return artifacts


def write_review(destination, run):
    publish_directory(destination, review_artifacts(run))
    print(f"{run['status']}: {destination / 'report.md'}")
    for error in run["adjudication"].get("validation_errors", []):
        print(f"Adjudication {error['stage']}.{error['path']}: "
              f"{error.get('message', error['error_type'])}", file=sys.stderr)
        if error.get("coverage"):
            coverage = error["coverage"]
            print(f"Expected {coverage['expected_count']}; returned {coverage['returned_count']}; "
                  f"missing: {', '.join(coverage['missing_group_ids']) or 'none'}; "
                  f"duplicate: {', '.join(coverage['duplicate_group_ids']) or 'none'}.", file=sys.stderr)
    if run.get("recovery", {}).get("reused_observation"):
        print("Reused four reviewer results and validated observations; only the decision stage was retried.")
    return 0 if run["status"] == "complete" else 2


def retry_adjudication_command(args):
    if args.out.exists() or args.out.is_symlink():
        raise ValueError("Choose a new output directory.")
    source_bytes = (args.run / "run.json").read_bytes()
    source = json.loads(source_bytes)
    verify_run(source)
    if sha256(args.draft.read_bytes()) != source["draft_sha256"]:
        raise ValueError("The draft differs from the reviewed version. Run the review again.")
    from .knowledge_sources import assert_knowledge_current
    assert_knowledge_current(source["snapshot"].get("knowledge_context"), **knowledge_options(args))
    provider = task_provider(load_config(args.config))
    recovered = retry_adjudication(source, provider, sha256(source_bytes))
    return write_review(args.out, recovered)


def selected_changes(run: dict, draft_hash: str, requested: list[str]) -> list[dict]:
    if run.get("draft_sha256") != draft_hash:
        raise ValueError("The draft differs from the reviewed version. Run the review again.")
    if len(set(requested)) != len(requested):
        raise ValueError("List each approved change once.")
    by_id = {change["id"]: change for change in validated_changes(run)}
    if set(requested) - by_id.keys():
        raise ValueError("At least one selected change ID is unavailable for approval.")
    return [by_id[change_id] for change_id in requested]


def approval_artifacts(run_bytes, original, requested, *, keep=False):
    """Validate a frozen review and produce a revision/receipt without file I/O."""
    run = json.loads(run_bytes)
    if not isinstance(run, dict):
        raise ValueError("The review record must be a JSON object.")
    draft_hash = sha256(original)
    changes = selected_changes(run, draft_hash, requested)
    revised = apply_changes(original.decode("utf-8"), changes).encode("utf-8")
    approval = json_bytes({
        "schema_version": RUN_VERSION,
        "approved_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_sha256": sha256(run_bytes),
        "run_id": run["run_id"],
        "input_sha256": run["input_sha256"],
        "ledger_sha256": fingerprint(run["ledger"]),
        "audit_head_sha256": run["audit"][-1]["sha256"],
        "parent_run_sha256": run["round"]["parent_run_sha256"],
        "draft_sha256": draft_hash,
        "revised_sha256": sha256(revised),
        "approved_change_ids": requested,
        "author_action": "keep" if keep else "approve_selected_changes",
        "approved_changes": changes,
        "adjudication_sha256": fingerprint(run["adjudication"]),
    })

    return revised, approval


def approve_command(args: argparse.Namespace) -> int:
    if args.out == args.draft or args.out.resolve() == args.draft.resolve():
        raise ValueError("Choose a separate path for the revised draft.")
    receipt = args.out.with_name(args.out.name + ".approval.json")
    if any(path.exists() or path.is_symlink() for path in (args.out, receipt)):
        raise ValueError("Choose a new output path for both the revision and its receipt.")
    from .knowledge_sources import assert_knowledge_current
    source = json.loads((args.run / "run.json").read_bytes())
    assert_knowledge_current(source["snapshot"].get("knowledge_context"), **knowledge_options(args))
    revised, approval = approval_artifacts((args.run / "run.json").read_bytes(), args.draft.read_bytes(),
                                          args.change or [], keep=args.keep)
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

    print(f"Approved {len(args.change or [])} change(s): {args.out}")
    return 0


def generate_command(args):
    if args.out.exists() or args.out.is_symlink():
        raise ValueError("Choose a new output directory.")
    config = load_config(args.config)
    draft, record = generate_draft(task_provider(config, "writer"), read_json(args.materials),
                                  args.requirements.read_text(encoding="utf-8"),
                                  args.preferences.read_text(encoding="utf-8") if args.preferences else "")
    publish_directory(args.out, {"draft.txt": draft.encode(), "generation.json": json_bytes(record)})
    print(f"Draft awaiting author verification: {args.out / 'draft.txt'}")
    return 0


def feedback_command(args):
    run_bytes = (args.run / "run.json").read_bytes()
    run = json.loads(run_bytes)
    verify_run(run)
    available = {i["issue_id"] for i in run["issues"]}
    if set(args.issue) - available or len(set(args.issue)) != len(args.issue) or not args.reason.strip():
        raise ValueError("Feedback needs unique issue IDs from this run and a nonempty reason.")
    record = {"version": "feedback-1", "run_sha256": sha256(run_bytes), "input_sha256": run["input_sha256"],
              "decisions": [{"issue_id": i, "decision": args.decision, "reason": args.reason} for i in args.issue]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("xb") as handle:
        os.chmod(args.out, 0o600)
        handle.write(json_bytes(record))
    print(f"Author feedback: {args.out}")
    return 0


def evaluate_command(args):
    if args.out.exists() or args.out.is_symlink():
        raise ValueError("Choose a new output directory.")
    dataset = read_json(args.dataset)
    artifacts = {}
    if args.command == "benchmark":
        from .benchmark import benchmark
        predictions, artifacts = benchmark(dataset, load_config(args.config), args)
    else:
        predictions = read_json(args.predictions)
    report = evaluate(dataset, predictions, args.bootstrap, args.seed)
    artifacts.update({"predictions.json": json_bytes(predictions), "metrics.json": json_bytes(report),
                      "report.md": render_evaluation(report).encode()})
    publish_directory(args.out, artifacts)
    print(f"Evaluation: {args.out / 'report.md'}; {report['case_count']} cases, {report['prediction_runs']} runs")
    return 0


def annotation_command(args):
    dataset = read_json(args.dataset)
    if args.command == "annotation-template":
        pack, labels = annotation_pack(dataset, args.annotator)
        artifacts = {"blind-cases.json": json_bytes(pack), "labels.json": json_bytes(labels)}
    else:
        artifacts = {"agreement.json": json_bytes(agreement(dataset, [read_json(p) for p in args.annotations]))}
    publish_directory(args.out, artifacts)
    print(f"Annotations: {args.out}")
    return 0


def _revision_source(args):
    raw = (args.run / "run.json").read_bytes()
    run = json.loads(raw)
    verify_run(run)
    if sha256(args.draft.read_bytes()) != run["draft_sha256"]:
        raise ValueError("The source draft changed after review; run the review again.")
    return raw, run


def revision_plan_command(args):
    from .revision import prepare_revision_plan
    raw, run = _revision_source(args)
    decisions = read_json(args.decisions)
    if isinstance(decisions, dict):
        decisions = decisions.get("decisions")
    facts = read_json(args.facts, [])
    if isinstance(facts, dict):
        facts = facts.get("facts", facts.get("confirmed_facts"))
    from .knowledge_sources import prepare_knowledge
    knowledge = prepare_knowledge(run["snapshot"]["draft"], **knowledge_options(args))
    knowledge_notice(knowledge)
    plan = prepare_revision_plan(run, sha256(raw), decisions, facts, read_json(args.answers, {}),
                                 args.essay_type, args.writing_sample.read_text() if args.writing_sample else "", args.word_limit,
                                 read_json(getattr(args, "glossary", None), []), knowledge_context=knowledge)
    template = {"decisions": [{key: row[key] for key in ("finding_id", "decision", "reason")} for row in plan["findings"]]}
    publish_directory(args.out, {"plan.json": json_bytes(plan), "decisions.json": json_bytes(template),
                                "knowledge.json": json_bytes(plan["knowledge_context"]),
                                "technical-context.json": json_bytes(plan["technical_context"]),
                                "questions.json": json_bytes({"unresolved_questions": plan["unresolved_questions"], "blockers": plan["blockers"]})})
    print(f"Revision plan: {args.out / 'plan.json'}")
    print(f"Author decisions: {args.out / 'decisions.json'}; unresolved: {len(plan['unresolved_questions'])}; blockers: {len(plan['blockers'])}")
    return 0


def revise_command(args):
    from .providers import FixtureProvider
    from .revision import revise_document, revision_artifacts
    from .config import revision_provider
    raw, run = _revision_source(args)
    plan_path = args.plan / "plan.json" if args.plan.is_dir() else args.plan
    plan = json.loads(plan_path.read_text())
    if plan["review_sha256"] != sha256(raw):
        raise ValueError("The reviewed run changed after planning; prepare a new revision plan.")
    from .knowledge_sources import assert_knowledge_current
    assert_knowledge_current(plan.get("knowledge_context"), **knowledge_options(args))
    knowledge_notice(plan.get("knowledge_context"))
    provider = FixtureProvider(args.fixtures) if args.fixtures else revision_provider(load_config(args.config))
    record = revise_document(run, plan, provider)
    publish_directory(args.out, revision_artifacts(record))
    print(f"{record['status']}: {args.out / 'candidate.txt'}")
    print(f"Inspect {args.out / 'full.diff'}, {args.out / 'trace.json'} and {args.out / 'validation.json'} before accepting.")
    return 0 if record["status"] == "ready" else 2


def finalize_revision_command(args):
    from .revision import finalize_revision
    raw, run = _revision_source(args)
    path = args.revision / "revision.json" if args.revision.is_dir() else args.revision
    record = json.loads(path.read_text())
    if record["plan"]["review_sha256"] != sha256(raw) or fingerprint(record["source_review"]) != fingerprint(run):
        raise ValueError("The reviewed run changed; regenerate the version-bound revision.")
    from .knowledge_sources import assert_knowledge_current
    assert_knowledge_current(record["plan"].get("knowledge_context"), **knowledge_options(args))
    candidate_sha = sha256(args.candidate.read_bytes()) if args.candidate else args.candidate_sha256
    text, receipt = finalize_revision(record, candidate_sha, args.plan_sha256)
    if args.out.resolve() == args.draft.resolve():
        raise ValueError("Choose a new path for the accepted full essay.")
    receipt_path = args.out.with_name(args.out.name + ".revision-acceptance.json")
    if any(p.exists() or p.is_symlink() for p in (args.out, receipt_path)):
        raise ValueError("Choose new output paths for the accepted essay and revision acceptance receipt.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".full-revision-", dir=args.out.parent) as temporary:
        essay_file, receipt_file = Path(temporary) / "essay", Path(temporary) / "receipt"
        essay_file.write_bytes(text.encode())
        receipt_file.write_bytes(json_bytes(receipt))
        essay_file.chmod(0o600)
        receipt_file.chmod(0o600)
        os.link(receipt_file, receipt_path)
        try:
            os.link(essay_file, args.out)
        except OSError:
            receipt_path.unlink()
            raise
    print(f"Accepted full essay: {args.out}")
    print(f"Revision acceptance receipt: {receipt_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and approve changes to an admissions essay.")
    commands = parser.add_subparsers(dest="command", required=True)
    def add_knowledge_options(command):
        command.add_argument("--repo", action="append", help="Override default sources with NAME=PATH; repeat for multiple repositories.")
        command.add_argument("--knowledge-config", type=Path, help="Alternate local knowledge TOML configuration.")
        command.add_argument("--no-knowledge", action="store_true", help="Explicitly disable repository retrieval for this run.")
    knowledge = commands.add_parser("knowledge", help="Refresh, inspect or query the local read-only repository index.")
    knowledge_actions = knowledge.add_subparsers(dest="knowledge_action", required=True)
    for action in ("status", "refresh", "query"):
        subcommand = knowledge_actions.add_parser(action)
        subcommand.add_argument("--config", type=Path)
        subcommand.add_argument("--out", type=Path, help="Write a new private artifact directory instead of printing JSON.")
        add_knowledge_options(subcommand)
        if action == "query":
            query = subcommand.add_mutually_exclusive_group(required=True)
            query.add_argument("--draft", type=Path)
            query.add_argument("--claim")
    serve = commands.add_parser("serve", help="Open the web interface and local HTTP API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8788)
    serve.add_argument("--data-dir", type=Path, default=Path("runs/web"))
    serve.add_argument("--config", type=Path)
    serve.add_argument("--password-file", type=Path)
    review = commands.add_parser("review", help="Run the independent editorial reviewers.")
    review.add_argument("--draft", type=Path, required=True)
    review.add_argument("--requirements", type=Path, required=True)
    review.add_argument("--preferences", type=Path)
    review.add_argument("--evidence-dir", type=Path, help="Directory of .txt or .md evidence files.")
    review.add_argument("--provider", choices=("fixture", "openai"))
    review.add_argument("--fixtures", type=Path, help="Directory of prewritten reviewer JSON files.")
    review.add_argument("--model", help="OpenAI model supporting Structured Outputs.")
    review.add_argument("--out", type=Path, required=True, help="New review output directory.")
    review.add_argument("--config", type=Path)
    review.add_argument("--claims", type=Path, help="Author-verified claim manifest JSON.")
    review.add_argument("--sources", type=Path, help="Source metadata JSON keyed by evidence filename.")
    review.add_argument("--as-of", help="ISO date for checking evidence expiry; defaults to today's date.")
    review.add_argument("--parent-run", type=Path)
    review.add_argument("--approval-receipt", type=Path)
    review.add_argument("--feedback", type=Path, help="Version-bound author issue decisions from feedback command.")
    review.add_argument("--max-rounds", type=int)
    review.add_argument("--author-ready", action="store_true")
    review.add_argument("--reverse-opinions", action="store_true", help="Order-sensitivity experiment for adjudication.")
    retry = commands.add_parser("retry-adjudication", help="Reuse all four validated reviews and retry failed adjudication only.")
    retry.add_argument("--run", type=Path, required=True)
    retry.add_argument("--draft", type=Path, required=True)
    retry.add_argument("--config", type=Path, help="Workflow provider; defaults to offline rules.")
    retry.add_argument("--out", type=Path, required=True)

    approve = commands.add_parser("approve", help="Apply specifically approved changes to a new file.")
    approve.add_argument("--run", type=Path, required=True, help="Review output directory.")
    approve.add_argument("--draft", type=Path, required=True)
    selection = approve.add_mutually_exclusive_group(required=True)
    selection.add_argument("--change", action="append", help="Change ID, repeatable.")
    selection.add_argument("--keep", action="store_true", help="Explicitly retain this version and issue an empty approval receipt.")
    approve.add_argument("--out", type=Path, required=True, help="New revised draft file.")
    approve.add_argument("--config", type=Path)
    schema = commands.add_parser("schema", help="Print an output/input JSON Schema.")
    schema.add_argument("--kind", choices=("review", *SCHEMAS, "dataset", "predictions", "annotations", "revision-writing", "revision-verification"), default="review")
    plan_command = commands.add_parser("revision-plan", help="Prepare version-bound accept/reject/defer decisions for full-essay revision.")
    plan_command.add_argument("--run", type=Path, required=True)
    plan_command.add_argument("--config", type=Path, help="Optional model/knowledge TOML; knowledge defaults load without this flag.")
    plan_command.add_argument("--draft", type=Path, required=True)
    plan_command.add_argument("--decisions", type=Path)
    plan_command.add_argument("--facts", type=Path, help="Author-confirmed fact list, with optional exact evidence citations.")
    plan_command.add_argument("--answers", type=Path, help="Confirmed applicant answers keyed by finding ID.")
    plan_command.add_argument("--essay-type", choices=("sop", "personal_statement", "short_answer"), default="sop")
    plan_command.add_argument("--writing-sample", type=Path, help="Voice reference only; never a factual source.")
    plan_command.add_argument("--word-limit", type=int)
    plan_command.add_argument("--glossary", type=Path, help="Optional JSON terminology list: term, definition, aliases. Not factual evidence.")
    plan_command.add_argument("--out", type=Path, required=True, help="New plan directory with plan, decision template and questions.")
    revise = commands.add_parser("revise", help="Run substantive revision, polishing, Humanizer, then read-only verification.")
    revise.add_argument("--run", type=Path, required=True)
    revise.add_argument("--draft", type=Path, required=True)
    revise.add_argument("--plan", type=Path, required=True)
    revision_mode = revise.add_mutually_exclusive_group(required=True)
    revision_mode.add_argument("--config", type=Path)
    revision_mode.add_argument("--fixtures", type=Path)
    revise.add_argument("--out", type=Path, required=True)
    final = commands.add_parser("finalize-revision", help="Accept an exact verified full candidate into an independent new essay.")
    final.add_argument("--run", type=Path, required=True)
    final.add_argument("--draft", type=Path, required=True)
    final.add_argument("--revision", type=Path, required=True)
    final_selection = final.add_mutually_exclusive_group(required=True)
    final_selection.add_argument("--candidate", type=Path, help="The candidate.txt file actually inspected by the author.")
    final_selection.add_argument("--candidate-sha256")
    final.add_argument("--plan-sha256")
    final.add_argument("--out", type=Path, required=True)
    final.add_argument("--config", type=Path)
    for command in (review, retry, approve, plan_command, revise, final):
        add_knowledge_options(command)
    generate = commands.add_parser("generate", help="Compose a draft from author-confirmed materials.")
    generate.add_argument("--materials", type=Path, required=True)
    generate.add_argument("--requirements", type=Path, required=True)
    generate.add_argument("--preferences", type=Path)
    generate.add_argument("--config", type=Path)
    generate.add_argument("--out", type=Path, required=True)
    feedback = commands.add_parser("feedback", help="Record author issue decisions without changing a draft.")
    feedback.add_argument("--run", type=Path, required=True)
    feedback.add_argument("--issue", action="append", required=True)
    feedback.add_argument("--decision", choices=("resolved", "deferred", "rejected"), required=True)
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--out", type=Path, required=True)
    for name in ("annotation-template", "agreement"):
        command = commands.add_parser(name, help="Prepare blind human labels or measure raw agreement.")
        command.add_argument("--dataset", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "annotation-template":
            command.add_argument("--annotator", required=True)
        else:
            command.add_argument("--annotations", type=Path, action="append", required=True)
    for name in ("evaluate", "benchmark"):
        command = commands.add_parser(name, help="Score predictions." if name == "evaluate" else "Execute frozen evaluation cases.")
        command.add_argument("--dataset", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--bootstrap", type=int, default=500)
        command.add_argument("--seed", type=int, default=17)
        if name == "evaluate":
            command.add_argument("--predictions", type=Path, required=True)
        else:
            command.add_argument("--config", type=Path)
            command.add_argument("--provider", choices=("fixture", "openai"))
            command.add_argument("--model")
            command.add_argument("--fixtures", type=Path)
            command.add_argument("--strategy", choices=("single", "four", "adjudicated"), default="four")

    args = parser.parse_args(argv)
    try:
        if args.command == "knowledge":
            return knowledge_command(args)
        if args.command == "serve":
            from .webserver import serve_command
            return serve_command(args)
        if args.command == "review":
            return review_command(args)
        if args.command == "retry-adjudication":
            return retry_adjudication_command(args)
        if args.command == "approve":
            return approve_command(args)
        if args.command == "generate":
            return generate_command(args)
        if args.command == "revision-plan":
            return revision_plan_command(args)
        if args.command == "revise":
            return revise_command(args)
        if args.command == "finalize-revision":
            return finalize_revision_command(args)
        if args.command == "feedback":
            return feedback_command(args)
        if args.command in {"evaluate", "benchmark"}:
            return evaluate_command(args)
        if args.command in {"annotation-template", "agreement"}:
            return annotation_command(args)
        from .revision_schema import WRITING, VERIFY_REVISION
        print(json.dumps(review_schema() if args.kind == "review" else {**SCHEMAS, "dataset": DATASET, "predictions": PREDICTIONS, "annotations": ANNOTATIONS, "revision-writing": WRITING, "revision-verification": VERIFY_REVISION}[args.kind], ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ReviewerError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, AttributeError):
        print("Error: A configuration, receipt or run record is malformed.", file=sys.stderr)
        return 1
