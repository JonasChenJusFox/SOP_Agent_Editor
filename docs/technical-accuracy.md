# Technical terminology and accuracy

Full-essay revision now freezes technical context before any writing call. It
records methods, models, metrics, data sources, libraries, system components,
and applicant/team contributions from the original draft, supplied evidence,
and explicitly confirmed applicant facts and answers.
Every extracted term and claim has a source type, paragraph ID or evidence
filename, exact passage, and offsets relative to that source. Passage-based claim
IDs remain stable when a glossary adds terminology annotations.

The SOP Reviser, Language Polisher, and Humanizer receive the same context.
Specialized terminology is not an AI-writing defect. The prompts explicitly
preserve retrieval versus ranking, embeddings versus fine-tuning, an interface
versus an underlying model, data splits, metric/value associations, negation,
and individual versus team responsibility.

Confirmed material is available through exact `confirmed_fact` source IDs and
passages. An applicant may have confirmed backend, retrieval, or interface work
that the original draft omitted; absence from the original alone does not make
that declared addition unsupported. The check still preserves its actual scope
and distinguishes applicant confirmation from independently verified evidence.
Unconfirmed material, writing samples, glossary definitions, reviewer advice,
and decision reasons cannot authorize new facts or expanded contributions.

Application requirements are kept separately as terminology-only context. A
contextual target-degree label such as SM or S.M. is not automatically a newly
invented technical method. Application wording cannot establish that the
applicant earned that qualification, developed a system, or achieved a metric.

Writers flag uncertain edits using `technical_flags`. Flagged original wording
must remain intact in that pass and later writing passes. If the offline checks
detect a risky change, the application retains the previous safe candidate,
stops further writing, and saves the proposed edit separately with source-backed
issues. It never silently replaces a model's technical claim with an invented
correction.

When an early writing proposal is rejected, Humanizer and the final read-only
check have not run yet. Their pending state is a consequence of the first
blocking issue, not several additional model failures. Inspect the proposed and
original passages, confirm the actual contribution or source material, then
prepare a new plan. Do not disable technical checks to release the old candidate.

After Humanizer, the existing **read-only final verifier** also returns
`technical_checks`. This is part of the final verification call, not an extra
writing pass or an additional model request. Every extracted draft claim needs
one result: exact preservation, a supported equivalent change, or a reported
altered, unsupported, or ambiguous claim. Checks cite the candidate and frozen
draft/evidence passages. Local checks validate coverage, exact quotes, known
IDs, common technical distinctions, metric/value associations and contribution
bindings. A positive general verification result cannot override technical
issues or missing technical checks.

## Optional project glossary

On the website, expand the full-revision area after review and add glossary
rows: term, definition, and optional aliases. Prepare a new plan after editing
them. The extracted context and technical report are shown in separate panels,
with downloads. Changing the glossary invalidates earlier plans and acceptance.

The CLI accepts a JSON list through `revision-plan --glossary`. For example:

```json
[
  {
    "term": "replay tool",
    "definition": "The tool that replays saved input messages for debugging; this is not model training.",
    "aliases": ["message replay tool"]
  }
]
```

Use the actual frozen review and its actual decisions/facts/answers:

```bash
source .venv/bin/activate
admissions-editor revision-plan \
  --run runs/my-sop-review \
  --draft private/essay/draft.txt \
  --decisions private/essay/decisions.json \
  --facts private/essay/facts.json \
  --answers private/essay/answers.json \
  --glossary private/essay/glossary.json \
  --out runs/my-sop-technical-plan
cat runs/my-sop-technical-plan/technical-context.json
```

Continue with `revise --plan runs/my-sop-technical-plan --config
examples/online.toml`, inspect the candidate, Diff and technical report, then
explicitly accept a ready version with `finalize-revision`. Complete commands
are in [revision.md](revision.md). The glossary is optional; an omitted glossary
is `[]`. It is terminology guidance, never proof that the applicant trained a
model, collected data, achieved a result, or owned a team contribution. Aliases
cannot merge known distinct operations such as retrieval and ranking.

Limits: 128 entries; 16 aliases per entry; 120 bytes per term/alias, 4,000 bytes
per definition, 32 KB total. Existing web request limits also apply. No new API
key, external skill, database, package, or service is required. Online revision
continues to use the existing OpenAI model configuration; offline cases use no
network. Local artifacts remain in the chosen `--out` directory; cloud artifacts
reuse the existing D1 files storage with no migration.

## Reproduce the offline checks

From the repository root with the Python environment installed:

```bash
source .venv/bin/activate
python scripts/technical_demo.py --out runs/my-technical-demo
cat runs/my-technical-demo/validation.json
python scripts/revision_demo.py --out runs/my-technical-revision
cat runs/my-technical-revision/revision/technical-context.json
cat runs/my-technical-revision/revision/technical-accuracy.json
python -m unittest discover -s tests -p 'test_technical*.py' -v
```

Verify the existing Cloudflare deployment with the optional example glossary
(synthetic fixtures only; no model calls):

```bash
python scripts/cloud_revision_smoke.py \
  --url https://admissions-editor-cloud.jonas-aitherpath.workers.dev \
  --password-file private/web-password.txt \
  --glossary examples/revision/glossary.json \
  --out runs/verification/my-cloud-technical-check.json
```

Choose new output directories for each run. The first script covers 12 synthetic
scenarios: 5 safe style rewrites and 7 changes to technical meaning. They include
swapping ownership when both retrieval and ranking already appear in the source,
turning embeddings usage into fine-tuning, promoting interface work to model
development, swapping metric values without changing the number set, changing
data splits or libraries, and preserving project-specific terminology. These
are deterministic behavior tests, not model accuracy or human quality scores.

The full workflow tests additionally exercise all three writing stages, risky
proposal rejection, propagation of preservation flags, source forgery, missing
checks, glossary changes, read-only verification and blocked final acceptance.

| Artifact | Contents |
| --- | --- |
| `technical-context.json` | Extracted terms/claims, confirmed facts, terminology-only requirements, glossary, exact source passages and source hashes |
| `technical-accuracy.json` | Final technical checks, source-backed issues, writing flags and completion status |
| `rejected-technical-proposal.json` | A rejected writing proposal, when a risky change was detected; candidate retains previous wording |
| `revision.json` / `validation.json` | Technical result bound to the full revision record and release decision |
| `*.revision-acceptance.json` | Explicit author acceptance, including technical context/report hashes |

## Verification limits and older plans

Extraction and local checks use a finite offline vocabulary plus supplied
identifiers and glossary terms. They are conservative heuristics, not a complete
technical parser or proof of semantic equivalence. The final model verifier and
applicant must still assess unrecognized terminology, complex relations, scope,
and actual source support. The system does not output AI-detection percentages.

The output contract and revision record are now version `full-revision-4`, with
version 4 prompts and default repository retrieval described in [knowledge.md](knowledge.md).
Prepare a new plan from an existing frozen review before
using this feature. Old plans/acceptances cannot silently acquire new checks;
old files remain available, but regenerating a full revision is required for
acceptance under the new contract. Four-reviewer results can be reused without
repeating those review calls. No database migration is needed.

Actual test counts, cloud verification and the distinction between synthetic,
real model and pending human validation are recorded in
[validation.md](validation.md).
