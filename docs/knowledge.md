# Repository-local knowledge

Review and revision commands load repository knowledge by default. You do not
need `--repo` for normal use. Configuration is in [`knowledge.toml`](../knowledge.toml).
It is independent of model selection, so `--config examples/online.toml` continues
to work with the same default sources.

The current workspace has these copies, with source files and directories set
read-only:

| Source | Local directory | Repository / delivered commit |
| --- | --- | --- |
| NearBite | `private/knowledge/repos/nearbite` | `JonasChenJusFox/nearbite`, `95f783bc707be10d704c417b5845b6eca413c6b4` |
| AitherPath | `private/knowledge/repos/aitherpath` | `JonasChenJusFox/AitherPath-CloudflareAI`, `184a4c55d9aa170d9f134e5fca4ba91ffe2909ef` |

The writable cache is `private/knowledge/index.sqlite3`. It is local SQLite with
FTS5, not a database server or hosted vector store. Python's standard library is
sufficient. Source repositories, exported materials, SQLite files, journals, and
their Git histories are excluded from the editor's Git by `private/` and the
explicit `/private/knowledge/` ignore rule. Nothing in that directory is tracked
in the editor repository.

## Populate a fresh computer

The sources above are already populated in this workspace. On a fresh checkout,
run these macOS commands from the editor repository root:

```bash
source .venv/bin/activate
mkdir -p private/knowledge/repos
chmod 700 private/knowledge private/knowledge/repos
git -c core.hooksPath=/dev/null clone --depth 1 \
  https://github.com/JonasChenJusFox/nearbite.git \
  private/knowledge/repos/nearbite
git -c core.hooksPath=/dev/null clone --depth 1 \
  https://github.com/JonasChenJusFox/AitherPath-CloudflareAI.git \
  private/knowledge/repos/aitherpath
chmod -R a-w private/knowledge/repos/nearbite private/knowledge/repos/aitherpath
admissions-editor knowledge refresh
admissions-editor knowledge status
```

Do not run `clone` into an already populated directory. Cloning follows the remote
default branch; every retrieval records the actual checked-out commit and content
snapshot, so later updates cannot be mistaken for the delivered versions.

The editor only reads sources. It does not fetch, checkout, install dependencies,
run repository scripts, change files, or make source commits. The default copies
are also protected by filesystem permissions. A working-tree change in an
explicitly overridden, writable source is represented by a content snapshot hash
alongside its Git HEAD; the commit alone is not presented as proof of those bytes.

## Refresh after source updates

Index refresh and repository update are separate operations. Commands detect local
source-content changes and automatically refresh changed indexed files. To obtain
new upstream source code, explicitly update your private copies first:

```bash
chmod -R u+w private/knowledge/repos/nearbite
git -C private/knowledge/repos/nearbite -c core.hooksPath=/dev/null pull --ff-only
chmod -R a-w private/knowledge/repos/nearbite

chmod -R u+w private/knowledge/repos/aitherpath
git -C private/knowledge/repos/aitherpath -c core.hooksPath=/dev/null pull --ff-only
chmod -R a-w private/knowledge/repos/aitherpath

admissions-editor knowledge refresh
admissions-editor knowledge status
```

The temporary write permission is for your explicit source update. Editorial
commands continue to treat repositories as read-only. `knowledge status` checks
current bytes and reports stale cached versions without silently using them.
`knowledge refresh` explicitly refreshes even when `auto_refresh = false`.
If a used source changes after planning, revision and final acceptance fail;
prepare a new plan. Frozen historical reports keep their original citations.

## Preview retrieval and use the defaults

```bash
admissions-editor knowledge query \
  --claim 'NearBite uses SentenceTransformer embeddings for restaurant retrieval.'

admissions-editor knowledge query \
  --claim 'AitherPath has scheduled evaluation jobs and evaluation records.'

admissions-editor knowledge query --draft private/essay/draft.txt \
  --out runs/my-knowledge-preview

admissions-editor review \
  --config examples/online.toml \
  --draft private/essay/draft.txt \
  --requirements private/essay/requirements.txt \
  --preferences private/essay/preferences.txt \
  --evidence-dir private/essay/evidence \
  --out runs/my-repository-review

cat runs/my-repository-review/knowledge.json
cat runs/my-repository-review/report.md

admissions-editor revision-plan \
  --run runs/my-repository-review --draft private/essay/draft.txt \
  --decisions private/essay/decisions.json \
  --facts private/essay/facts.json --answers private/essay/answers.json \
  --out runs/my-repository-plan

admissions-editor revise \
  --run runs/my-repository-review --draft private/essay/draft.txt \
  --plan runs/my-repository-plan --config examples/online.toml \
  --out runs/my-repository-revision

cat runs/my-repository-revision/knowledge.json
cat runs/my-repository-revision/technical-accuracy.json
```

Create the actual essay files and author decisions first, using
[the revision guide](revision.md). Online commands require the existing
`OPENAI_API_KEY` and `OPENAI_MODEL` to be explicitly loaded from `.env`; knowledge
indexing and querying do not require a model or credentials. After inspecting a
ready candidate and Diff, use the existing `finalize-revision` command. The host
rechecks source versions and each cited passage before writing a new final essay.

The four reviewers receive the same frozen passages and claim-to-passage mapping.
They receive no other reviewers' opinions. Revision plans refresh retrieval and
replace historical KB passages while retaining manually supplied evidence;
source-version changes are reported. SOP Reviser, Language Polisher, Humanizer,
and the final read-only technical verifier receive the selected evidence and
provenance. No whole repository or SQLite database is sent to a model. Relevant
retrieved excerpts are sent when you explicitly run an online review/revision.

## Overrides and additional authorized materials

Use `--repo NAME=PATH` repeatedly to replace the default source list for a command:

```bash
admissions-editor knowledge query \
  --repo nearbite=/absolute/path/to/another-nearbite-checkout \
  --claim 'NearBite restaurant retrieval uses embeddings.'
```

The same flags are available on `review`, `revision-plan`, `revise`,
`finalize-revision`, `approve`, and `retry-adjudication`. Use the same source
configuration for the plan and its later commands. A source change cannot be
hidden by disabling knowledge after a plan was made. To intentionally skip local
knowledge, prepare a new run/plan with `--no-knowledge`.

For a lasting override, edit `knowledge.toml`, use
`--knowledge-config private/knowledge/sources.toml`, or add `[knowledge]` and
`[[knowledge.sources]]` entries to the existing model TOML. File paths are relative
to that TOML; explicit `--repo` paths are relative to the editor project root.
`knowledge.sources` replaces the source list; partial scalar settings merge with
the defaults. The cache must remain inside this project's `private/knowledge/`
and outside every read-only source repository.

Authorized AitherPath materials can be a Git checkout of tracked text documents,
tests and evaluation artifacts, or a curated text export with an explicit
`.knowledge-manifest.json`. For a document-only source, configure its path under
the existing `aitherpath` name. Example export layout:

```text
private/knowledge/repos/aitherpath-materials/
  .knowledge-manifest.json
  notes/architecture.md
  evaluation/results.json
```

After copying only the files you are authorized to use, create the manifest from
an explicit allowlist:

```bash
python - <<'PY'
import hashlib, json
from pathlib import Path
root = Path('private/knowledge/repos/aitherpath-materials')
approved = ['notes/architecture.md', 'evaluation/results.json']
manifest = {
    'schema_version': 1,
    'repository': 'aitherpath',
    'commit_sha': None,
    'files': [{'path': name, 'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest()}
              for name in approved],
}
(root / '.knowledge-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
chmod -R a-w private/knowledge/repos/aitherpath-materials
```

Set the `aitherpath` source path in `knowledge.toml` to that export and refresh.
This example is a template; it does not invent or create your authorized notes.
Only listed files with matching hashes can be indexed. When editing an export,
update its approved manifest hashes before refreshing. Unapproved new files are
ignored; a changed approved file with an old hash makes the source stale/error
and prevents cached passages from being used. PDF/Word materials must first be
converted to reviewed plain text; the repository index does not execute OCR.

## Indexed content, provenance, and failure behavior

The index reads tracked Git source files, documentation, tests, and text
evaluation artifacts, including JSON/JSONL/CSV. It excludes untracked files,
symlinks, `.git`, secrets/environment files, private keys, dependencies,
generated/vendor/build output, binaries, and secret-like contents. Files over
512 KB are skipped, with a diagnostic; a source is limited to 10,000 eligible
files and 64 MB. Initial indexing of the delivered copies selected 84 NearBite
files and 157 AitherPath files; two oversized NearBite files were excluded.

Retrieval uses local lexical FTS ranking, project scoping, and conservative
technical/identifier anchors. Named project claims cannot borrow another
project's passages. Claims inherit a single project name from their paragraph
while their original wording remains unchanged in the report. Generic admissions
goals and reflections do not trigger unrelated code retrieval. A claim can have
zero matches; absence of a match is not evidence that the claim is false.
Defaults are at most three nonoverlapping passages per claim and 24 total,
with windows of up to 32 lines/6 KB. Relevance is heuristic and must be inspected.

Every passage contains repository name, commit SHA when available, content
snapshot hash, relative file path, line range, excerpt, file hash, and a stable
`KB-*` evidence ID. Reports map each essay claim to those IDs. Missing/stale
sources and unmatched claims are visible. Cached passages from missing or stale
sources are never returned. Before local revision/approval/acceptance, the host
checks the exact cited index rows and current versions; recomputing a JSON
checksum cannot authorize a forged excerpt or line location.

Code demonstrates implementation details. It does not establish who authored
them, whether they were deployed, measured impact, or exclusive personal
contribution. Those require applicant confirmation and appropriate supporting
records. Source text is quoted data, never instructions for the editor.

## Storage and initialization

| Item | Location / initialization |
| --- | --- |
| Default source configuration | `knowledge.toml`; no required environment variables |
| Source repositories/materials | `private/knowledge/repos/`; Git checkouts or verified exports, read-only |
| SQLite cache | `private/knowledge/index.sqlite3`; `admissions-editor knowledge refresh` initializes and refreshes it |
| Schema | `src/admissions_editor/knowledge.schema.sql`; `PRAGMA user_version=1`, automatic initial 0→1 setup |
| Tables | `sources`, `files`, `passages`, `passage_fts`; source identities, file hashes, exact chunks, local FTS5 |
| Model configuration | Existing `.env` / model TOML; only online editorial calls need API credentials |

No daemon or migration service is required. The index is disposable: if a future
version reports an unsupported schema, move the old index into a private backup
and run `knowledge refresh` to rebuild from read-only sources. Do not put the
cache inside a source repository. There is no hosted knowledge database.

## Reproducible verification and cloud boundary

```bash
python scripts/knowledge_demo.py --out runs/my-knowledge-demo
cat runs/my-knowledge-demo/validation.json
python -m unittest discover -s tests -p 'test_knowledge*.py' -v
git check-ignore private/knowledge/index.sqlite3 private/knowledge/repos/nearbite/README.md
git ls-files private/knowledge
```

The demo creates separate synthetic repositories under its output directory,
loads them through defaults without `--repo`, runs review→revision→technical
verification→acceptance, verifies citations, then simulates a source change and
tests stale-operation rejection and refresh. It never changes your real source
copies or calls a model. Built-in web/CLI demos explicitly disable private
knowledge to keep synthetic fixtures isolated; normal commands load it by default.

The local CLI and local Python web server can access this knowledge. The native
Cloudflare worker cannot read files on your Mac; it explicitly reports missing
local default sources. Deployment does not upload these repositories, the index,
or private materials. The cloud UI continues to accept manually supplied evidence.
This feature does not silently introduce a hosted source store. Current revision
records/prompts are version 3; prepare a new plan from an existing frozen review
to use the new evidence contract. Actual commands/results are in
[validation.md](validation.md).
