"""Private, refreshable SQLite index of read-only repository passages.

Only tracked Git files or explicitly manifested exports are indexed. Retrieval
is lexical and claim-specific; matching code is evidence to inspect, not proof
of an applicant's authorship, ownership, deployment, or measured outcome.
"""

import hashlib
import json
import math
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

CONTEXT_VERSION = "knowledge-context-1"
INDEX_VERSION = 1
DEFAULT_SOURCE_NAMES = ("nearbite", "aitherpath")
MAX_FILE_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILES = 10000
_TEXT_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".sql", ".md", ".mdx", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json", ".jsonl", ".csv", ".html", ".css", ".scss", ".svelte", ".vue", ".graphql", ".gql", ".r", ".R"}
_EXCLUDED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "vendor", "dist", "build", "coverage", ".next", ".nuxt", ".cache", "__pycache__", "private", "secrets", "credentials", ".aws", "logs", "uploads"}
_EXCLUDED_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "pipfile.lock", ".knowledge-manifest.json", ".ds_store"}
_STOP = set("a an the i me my we our us they their he she it its you your this that these those and or but of in on to for from as at by with is are was were be been being have has had do does did not no so then than into through while which who what when where how can could may might must will would should more most very also about because after before during between over under one two three four five use used using built build building implement implemented implementing develop developed developing project projects application applications applicant applicants team teams work worked working software system systems tool tools code repository repositories relevant source sources claim claims specific personally individual result results demonstrate demonstrates explain describe".split())
_WEAK = {"interface", "model", "data", "test", "tests", "documentation", "evaluation", "component", "pipeline", "feature", "frontend", "backend"}
_STRONG = {"retrieval", "ranking", "embeddings", "finetuning", "training", "faiss", "bert", "ndcg", "mrr", "auc", "auroc", "precision", "recall", "f1", "latency", "p95", "p99", "pytorch", "tensorflow", "rag", "bm25", "xgboost"}
_UNSCOPED_ANCHORS = {"faiss", "bm25", "bert", "pytorch", "tensorflow", "xgboost", "ndcg", "mrr", "auroc", "auc", "f1", "rag", "sklearn", "numpy", "pandas", "pgvector", "postgresql", "elasticsearch", "opensearch", "llama", "distilbert", "tfidf"}
_STOP.update("want wanted study studied interested interest useful unreliable errors error inputs input outputs output messages message investigate investigated reviewing reviewed grow growth graduate school university wrote write written implemented saved".split())
_SYNONYMS = {"retrieve": "retrieval", "retrieves": "retrieval", "retrieved": "retrieval", "retrieving": "retrieval", "retriever": "retrieval", "rank": "ranking", "ranks": "ranking", "ranked": "ranking", "ranker": "ranking", "rerank": "ranking", "reranking": "ranking", "embedding": "embeddings", "finetune": "finetuning", "finetuned": "finetuning", "fine": "fine", "train": "training", "trained": "training", "trains": "training", "metrics": "metric", "models": "model", "datasets": "dataset", "interfaces": "interface", "ui": "interface", "recommendations": "recommendation", "recommender": "recommendation", "recommend": "recommendation"}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(value):
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def default_sources(project_root):
    root = Path(project_root) / "private/knowledge/repos"
    return [{"name": name, "path": str(root / name), "required": True} for name in DEFAULT_SOURCE_NAMES]


def _tokens(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"fine[\s_-]*tun(?:e|es|ed|ing)", "finetuning", text, flags=re.I)
    result = []
    for value in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        token = value.casefold()
        token = _SYNONYMS.get(token, token)
        if len(token) >= 2 and token not in _STOP:
            result.append(token)
    return result


def _source_pattern(name):
    compact = re.sub(r"[^A-Za-z0-9]", "", name)
    return r"(?<![A-Za-z0-9])" + r"[\W_]*".join(re.escape(c) for c in compact) + r"(?![A-Za-z0-9])" if compact else r"(?!)"


def _named_sources(text, names):
    matches = set()
    for name in names:
        if re.search(_source_pattern(name), text, re.I):
            matches.add(name)
    return matches


def _query_identifiers(text):
    identifiers = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*_[A-Za-z0-9_]+\b|\b[A-Za-z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*\b|\b[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+\b", text))
    identifiers.update(re.findall(r"`([^`\n]{8,120})`", text))
    return {value.casefold() for value in identifiers if len(value) >= 8}


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(p in {"", "."} for p in path.parts):
        return None
    return path


def _eligible(value):
    path = _safe_relative(value)
    if path is None:
        return False
    parts = [part.casefold() for part in path.parts]
    name = parts[-1]
    if any(part in _EXCLUDED_DIRS for part in parts[:-1]) or name in _EXCLUDED_NAMES:
        return False
    if name.startswith((".env", "id_rsa", "id_ed25519")) or re.search(r"(?:secret|credential|private[-_]?key)(?:s)?(?:\.|$)", name):
        return False
    if name.endswith((".min.js", ".min.css", ".map", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx")):
        return False
    return path.suffix.casefold() in {s.casefold() for s in _TEXT_SUFFIXES} or name in {"makefile", "dockerfile", "cmakelists.txt", "license", "readme"}


def _contains_secret(text):
    if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|AKIA[A-Z0-9]{16})\b", text):
        return True
    for match in re.finditer(r"(?:api[_-]?key|client[_-]?secret|password|access[_-]?token)\s*[:=]\s*['\"]([A-Za-z0-9+/=_-]{20,})['\"]", text, re.I):
        if not match.group(1).upper().startswith(("REPLACE_", "YOUR_", "EXAMPLE_", "SYNTHETIC_")):
            return True
    return False


def _kind(path):
    parts = set(PurePosixPath(path).parts)
    if any(part.casefold() in {"eval", "evals", "evaluation", "evaluations", "benchmarks", "metrics", "results"} for part in parts):
        return "evaluation"
    if any(part.casefold() in {"test", "tests", "__tests__", "spec", "specs"} for part in parts) or re.search(r"(?:^|[/_.-])(?:test|spec)(?:[_.-]|$)", path, re.I):
        return "test"
    return "documentation" if PurePosixPath(path).suffix.casefold() in {".md", ".mdx", ".rst", ".txt"} else "source"


def _git(root, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
                            env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Cannot read Git metadata; no source files were changed.")
    return result.stdout


def _tracked_paths(root, name):
    if (root / ".git").exists():
        commit = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
            raise ValueError("Invalid Git commit identifier.")
        raw = _git(root, "ls-files", "--cached", "-z")
        paths = sorted(set(raw.decode("utf-8").split("\0")) - {""})
        return commit, paths, None
    manifest_path = root / ".knowledge-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Source is not a Git repository or an explicitly authorized manifested export.")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("repository", manifest.get("name")) != name:
        raise ValueError("Export manifest must name this repository and schema_version 1.")
    commit = manifest.get("commit_sha")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40,64}", commit)):
        raise ValueError("Export manifest commit_sha must be a Git hash or null.")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) > MAX_SOURCE_FILES:
        raise ValueError("Export manifest needs a bounded file list.")
    hashes = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"} or _safe_relative(entry["path"]) is None or \
                entry["path"] in hashes or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
            raise ValueError("Invalid or duplicate export manifest file.")
        hashes[entry["path"]] = entry["sha256"]
    return commit, sorted(hashes), hashes


def _scan(source):
    root = source["path"]
    if root.is_symlink():
        raise ValueError("Source repository must not be a symlink.")
    if not root.is_dir():
        return None
    commit, paths, approved_hashes = _tracked_paths(root, source["name"])
    if len(paths) > MAX_SOURCE_FILES:
        raise ValueError("Repository exceeds the tracked file limit.")
    files, signatures, skipped, total = [], {}, {}, 0
    for value in paths:
        if not _eligible(value):
            continue
        path = root / value
        ancestors = [root.joinpath(*PurePosixPath(value).parts[:i]) for i in range(1, len(PurePosixPath(value).parts) + 1)]
        if any(p.is_symlink() for p in ancestors) or not path.resolve().is_relative_to(root.resolve()):
            skipped["symlink_or_escape"] = skipped.get("symlink_or_escape", 0) + 1
            continue
        if not path.exists():
            if approved_hashes is not None:
                raise ValueError("An approved exported file is missing; refresh the export.")
            continue  # A tracked deletion changes the content snapshot.
        if not path.is_file():
            continue
        stat = path.stat()
        if stat.st_size > MAX_FILE_BYTES:
            skipped["oversized"] = skipped.get("oversized", 0) + 1
            continue
        raw = path.read_bytes()
        file_hash = _digest(raw)
        if approved_hashes is not None and file_hash != approved_hashes[value]:
            raise ValueError("Exported source content differs from its authorized manifest; refresh the export.")
        signatures[value] = (stat.st_size, stat.st_mtime_ns)
        if b"\0" in raw:
            skipped["binary"] = skipped.get("binary", 0) + 1
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            skipped["non_utf8"] = skipped.get("non_utf8", 0) + 1
            continue
        if _contains_secret(text):
            skipped["secret_pattern"] = skipped.get("secret_pattern", 0) + 1
            continue
        total += len(raw)
        if total > MAX_SOURCE_BYTES:
            raise ValueError("Repository exceeds the indexed text limit.")
        files.append({"path": value, "sha256": file_hash, "text": text, "kind": _kind(value)})
    for value, signature in signatures.items():
        path = root / value
        stat = path.stat()
        if signature != (stat.st_size, stat.st_mtime_ns):
            raise ValueError("Source content changed during indexing; refresh again.")
    if (root / ".git").exists() and _git(root, "rev-parse", "--verify", "HEAD").decode().strip() != commit:
        raise ValueError("Source commit changed during indexing; refresh again.")
    snapshot = _fingerprint({"repository": source["name"], "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files]})
    return {"commit_sha": commit, "snapshot_hash": snapshot, "files": files, "skipped": skipped}


def _chunks(text, size=32, overlap=6):
    lines = text.splitlines(keepends=True)
    def passage(start, end):
        value = "".join(lines[start:end])
        # Omit only the terminal line ending; keep internal CRLF/LF verbatim.
        return value[:-2] if value.endswith("\r\n") else value[:-1] if value.endswith(("\r", "\n")) else value
    start = 0
    while start < len(lines):
        end = min(start + size, len(lines))
        # Bound excerpts without changing the text or its line citations.
        while end > start + 1 and len(passage(start, end).encode()) > 6000:
            end -= 1
        excerpt = passage(start, end)
        if excerpt.strip() and len(excerpt.encode()) <= 6000:
            yield start + 1, end, excerpt
        start = max(end - overlap, start + 1) if end < len(lines) else len(lines)


class KnowledgeIndex:
    def __init__(self, index_path, sources, *, auto_refresh=True):
        requested_index = Path(index_path).expanduser().absolute()
        if requested_index.is_symlink():
            raise ValueError("Knowledge index path must not be a symlink.")
        # macOS /var and /tmp are system symlinks; normalize their ancestors.
        self.path = requested_index.resolve()
        self.auto_refresh = auto_refresh
        if not isinstance(auto_refresh, bool):
            raise ValueError("auto_refresh must be boolean.")
        self.sources = []
        seen = set()
        if not isinstance(sources, (list, tuple)):
            raise ValueError("Knowledge sources must be a list.")
        for raw in sources:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", raw["name"]):
                raise ValueError("Knowledge source requires a safe repository name.")
            name = raw["name"]
            if name.casefold() in seen or not isinstance(raw.get("path"), (str, Path)):
                raise ValueError("Knowledge source names must be unique and have a local path.")
            seen.add(name.casefold())
            root = Path(raw["path"]).expanduser().absolute()
            if root.is_symlink():
                raise ValueError("Source repository must not be a symlink.")
            root = root.resolve()
            if self.path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Keep the writable SQLite index outside every read-only source repository.")
            self.sources.append({"name": name, "path": root, "required": raw.get("required", True)})

    @contextmanager
    def _connect(self):
        import sqlite3  # Local-only; frozen editorial contexts do not need SQLite.
        from importlib.resources import files
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            self.path.chmod(0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, INDEX_VERSION}:
                raise ValueError("Unsupported knowledge index version; create a new local index.")
            connection.executescript(files("admissions_editor").joinpath("knowledge.schema.sql").read_text())
            connection.execute(f"PRAGMA user_version={INDEX_VERSION}")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def refresh(self, *, force=True):
        """Compare content hashes and refresh changed files transactionally."""
        diagnostics, states = [], []
        with self._connect() as db:
            configured = {source["name"] for source in self.sources}
            for row in db.execute("SELECT name FROM sources").fetchall():
                if row["name"] not in configured:
                    self._remove_source(db, row["name"])
            for source in self.sources:
                name = source["name"]
                prior = db.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
                try:
                    scan = _scan(source)
                    message = ""
                    if scan is None:
                        state = "missing"
                        message = f"Knowledge source {name!r} is missing at {source['path']}; no passages from it were used."
                    elif not force and (prior is None or prior["status"] != "ready" or prior["snapshot_hash"] != scan["snapshot_hash"] or prior["commit_sha"] != scan["commit_sha"] or prior["path"] != str(source["path"])):
                        state = "stale"
                        message = f"Knowledge source {name!r} is stale or not indexed; refresh it before retrieval."
                    else:
                        state = "ready"
                    changed = bool(scan and (prior is None or prior["snapshot_hash"] != scan["snapshot_hash"] or prior["commit_sha"] != scan["commit_sha"] or prior["path"] != str(source["path"])))
                    if state == "ready":
                        db.execute("INSERT INTO sources(name,path,status) VALUES(?,?,'ready') ON CONFLICT(name) DO UPDATE SET path=excluded.path,status='ready'", (name, str(source["path"])))
                        old = {row["path"]: row["file_sha256"] for row in db.execute("SELECT path,file_sha256 FROM files WHERE repository=?", (name,))}
                        current = {file["path"] for file in scan["files"]}
                        for path in set(old) - current:
                            self._remove_file(db, name, path)
                        for file in scan["files"]:
                            if old.get(file["path"]) == file["sha256"] and prior and prior["path"] == str(source["path"]):
                                continue
                            self._remove_file(db, name, file["path"])
                            db.execute("INSERT INTO files VALUES(?,?,?,?)", (name, file["path"], file["sha256"], file["kind"]))
                            for start, end, excerpt in _chunks(file["text"]):
                                internal = _fingerprint([name, file["path"], file["sha256"], start, end])
                                db.execute("INSERT INTO passages VALUES(?,?,?,?,?,?)", (internal, name, file["path"], start, end, excerpt))
                                db.execute("INSERT INTO passage_fts VALUES(?,?)", (internal, " ".join(_tokens(excerpt))))
                        count = db.execute("SELECT COUNT(*) FROM passages WHERE repository=?", (name,)).fetchone()[0]
                        db.execute("UPDATE sources SET commit_sha=?,snapshot_hash=?,status='ready',diagnostic='',file_count=?,passage_count=? WHERE name=?", (scan["commit_sha"], scan["snapshot_hash"], len(scan["files"]), count, name))
                        if scan["skipped"]:
                            diagnostics.append(f"Knowledge source {name!r} excluded files: " + ", ".join(f"{key}={value}" for key, value in sorted(scan["skipped"].items())) + ".")
                    else:
                        db.execute("INSERT INTO sources(name,path,status,diagnostic) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET path=excluded.path,status=excluded.status,diagnostic=excluded.diagnostic", (name, str(source["path"]), state, message))
                    if message:
                        diagnostics.append(message)
                except (OSError, ValueError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as error:
                    state, changed = ("stale" if prior and prior["snapshot_hash"] else "error"), False
                    # Error text is controlled here; filesystem/API messages are not persisted.
                    detail = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else type(error).__name__
                    message = f"Knowledge source {name!r} is {state}: {detail}; no cached passages from it were used."
                    diagnostics.append(message)
                    db.execute("INSERT INTO sources(name,path,status,diagnostic) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET path=excluded.path,status=excluded.status,diagnostic=excluded.diagnostic", (name, str(source["path"]), state, message))
                row = dict(db.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone())
                row.pop("diagnostic")
                row["changed"] = changed
                states.append(row)
        ready = sum(s["status"] == "ready" for s in states)
        status = "ready" if states and ready == len(states) else "partial" if ready else "missing"
        return {"status": status, "sources": states, "diagnostics": diagnostics,
                "snapshot_sha256": _fingerprint([{k: v for k, v in s.items() if k != "changed"} for s in states])}

    @staticmethod
    def _remove_file(db, name, path):
        for row in db.execute("SELECT internal_id FROM passages WHERE repository=? AND path=?", (name, path)).fetchall():
            db.execute("DELETE FROM passage_fts WHERE internal_id=?", (row[0],))
        db.execute("DELETE FROM files WHERE repository=? AND path=?", (name, path))

    def _remove_source(self, db, name):
        for row in db.execute("SELECT path FROM files WHERE repository=?", (name,)).fetchall():
            self._remove_file(db, name, row[0])
        db.execute("DELETE FROM sources WHERE name=?", (name,))

    def validate_context(self, context):
        """Authenticate frozen passage contents against refreshed local sources.

        A recomputed JSON checksum cannot turn fabricated prose into repository
        evidence. Relevance and applicant attribution remain separate judgments.
        """
        if not isinstance(context, dict) or context.get("schema_version") != CONTEXT_VERSION or \
                context.get("snapshot_sha256") != _fingerprint({key: value for key, value in context.items() if key != "snapshot_sha256"}) or \
                not isinstance(context.get("passages"), list):
            raise ValueError("Invalid frozen knowledge context.")
        if not context["passages"]:
            return True
        refreshed = self.refresh()
        ready = {source["name"]: source for source in refreshed["sources"] if source["status"] == "ready"}
        claims = {claim.get("id", claim.get("claim_id")): claim for claim in context.get("claims", []) if isinstance(claim, dict)}
        with self._connect() as db:
            for passage in context["passages"]:
                if not isinstance(passage, dict):
                    raise ValueError("Invalid frozen knowledge passage.")
                source = ready.get(passage.get("repository"))
                if source is None:
                    raise ValueError("A used knowledge source is missing, stale, or no longer configured.")
                row = db.execute("SELECT p.*,f.file_sha256,f.kind FROM passages p JOIN files f ON f.repository=p.repository AND f.path=p.path WHERE p.repository=? AND p.path=? AND p.line_start=? AND p.line_end=?",
                                 (passage.get("repository"), passage.get("path"), passage.get("line_start"), passage.get("line_end"))).fetchone()
                if row is None:
                    raise ValueError("A frozen knowledge passage location is not present in the current index.")
                expected = {"repository": row["repository"], "commit_sha": source["commit_sha"], "snapshot_hash": source["snapshot_hash"],
                            "path": row["path"], "file_path": row["path"], "line_start": row["line_start"], "line_end": row["line_end"],
                            "excerpt": row["excerpt"], "file_sha256": row["file_sha256"], "kind": row["kind"]}
                expected_id = "KB-" + _fingerprint(expected)[:24]
                if passage.get("id") != expected_id or passage.get("evidence_id") != expected_id or \
                        any(passage.get(key) != value for key, value in expected.items()):
                    raise ValueError("A frozen knowledge passage differs from its current source, version, excerpt or line range.")
                for claim_id in passage.get("claim_ids", []):
                    claim = claims.get(claim_id)
                    if claim is None:
                        raise ValueError("Knowledge passage references an unknown essay claim.")
                    names = [s["name"] for s in self.sources]
                    named = _named_sources(claim.get("text", ""), names) or _named_sources(claim.get("query", claim.get("text", "")), names)
                    if named and passage["repository"] not in named:
                        raise ValueError("Knowledge passage belongs to a different explicitly named project.")
        return True

    def retrieve(self, claims, limit_per_claim=3, *, max_passages=24):
        """Refresh, then retrieve bounded passages independently for each claim."""
        if not isinstance(claims, list) or len(claims) > 128 or isinstance(limit_per_claim, bool) or not isinstance(limit_per_claim, int) or not 1 <= limit_per_claim <= 8 or not isinstance(max_passages, int) or not 1 <= max_passages <= 64:
            raise ValueError("Use at most 128 claims, 1..8 passages per claim and 1..64 total passages.")
        queries, ids = [], set()
        for item in claims:
            if not isinstance(item, dict):
                raise ValueError("Claims must contain an ID and text.")
            claim_id = item.get("id", item.get("claim_id"))
            text = item.get("text")
            query = item.get("query", text)
            if not isinstance(claim_id, str) or not claim_id or claim_id in ids or not isinstance(text, str) or not text.strip() or len(text.encode()) > 128 * 1024:
                raise ValueError("Claim IDs must be unique with nonempty bounded text.")
            if not isinstance(query, str) or not query.strip() or len(query.encode()) > 128 * 1024:
                raise ValueError("Claim query must be nonempty bounded text.")
            ids.add(claim_id)
            queries.append({"id": claim_id, "claim_id": claim_id, "text": text, "passage_ids": []})
            if query != text:
                queries[-1]["query"] = query
        report = self.refresh(force=self.auto_refresh)
        context = {"schema_version": CONTEXT_VERSION, "status": report["status"],
                   "sources": [{k: v for k, v in source.items() if k != "changed"} for source in report["sources"]],
                   "claims": queries, "passages": [], "diagnostics": list(report["diagnostics"])}
        selected = {}
        ready = {source["name"]: source for source in context["sources"] if source["status"] == "ready"}
        with self._connect() as db:
            total = max(db.execute("SELECT COUNT(*) FROM passages p JOIN sources s ON s.name=p.repository WHERE s.status='ready'").fetchone()[0], 1)
            for claim in queries:
                query = claim.get("query", claim["text"])
                names = [s["name"] for s in self.sources]
                named = _named_sources(claim["text"], names) or _named_sources(query, names)
                identifiers = _query_identifiers(query)
                scope_free_query = query
                for name in named:
                    scope_free_query = re.sub(_source_pattern(name), " ", scope_free_query, flags=re.I)
                tokens = sorted(set(_tokens(scope_free_query)))[:40]
                meaningful = set(tokens) - {s.casefold() for s in ready} - _WEAK
                if not meaningful:
                    continue
                if not named and not set(tokens).intersection(_UNSCOPED_ANCHORS) and not identifiers:
                    continue
                expression = " OR ".join('"' + token + '"' for token in tokens)
                rows = db.execute("SELECT p.*,f.file_sha256,f.kind,bm25(passage_fts) AS rank FROM passage_fts JOIN passages p ON p.internal_id=passage_fts.internal_id JOIN files f ON f.repository=p.repository AND f.path=p.path JOIN sources s ON s.name=p.repository WHERE passage_fts MATCH ? AND s.status='ready' ORDER BY rank,p.repository,p.path,p.line_start LIMIT 100", (expression,)).fetchall()
                frequencies = {token: db.execute("SELECT COUNT(*) FROM passage_fts WHERE passage_fts MATCH ?", ('"' + token + '"',)).fetchone()[0] for token in meaningful}
                ranked = []
                for row in rows:
                    source = ready.get(row["repository"])
                    if source is None or (named and row["repository"] not in named):
                        continue
                    hits = set(_tokens(row["excerpt"])) & set(tokens)
                    if not named and not hits.intersection(_UNSCOPED_ANCHORS) and not any(identifier in row["excerpt"].casefold() or identifier in row["path"].casefold() for identifier in identifiers):
                        continue
                    if not hits.intersection(meaningful) or (len(hits) < 2 and not hits.intersection(_STRONG) and len(meaningful) > 1):
                        continue
                    score = sum(1 + math.log((total + 1) / (frequencies.get(token, total) + 1)) for token in hits.intersection(meaningful))
                    ranked.append((score, row, source))
                ranked.sort(key=lambda entry: (-entry[0], entry[1]["repository"], entry[1]["path"], entry[1]["line_start"]))
                locations = []
                for score, row, source in ranked:
                    if any(repository == row["repository"] and path == row["path"] and max(start, row["line_start"]) <= min(end, row["line_end"]) for repository, path, start, end in locations):
                        continue
                    metadata = {"repository": row["repository"], "commit_sha": source["commit_sha"], "snapshot_hash": source["snapshot_hash"],
                                "path": row["path"], "file_path": row["path"], "line_start": row["line_start"], "line_end": row["line_end"],
                                "excerpt": row["excerpt"], "file_sha256": row["file_sha256"], "kind": row["kind"]}
                    passage_id = "KB-" + _fingerprint(metadata)[:24]
                    if passage_id not in selected and len(selected) >= max_passages:
                        continue
                    if passage_id not in selected:
                        selected[passage_id] = {"id": passage_id, "evidence_id": passage_id, **metadata,
                                                "relevance_score": round(score, 6), "claim_ids": []}
                    selected[passage_id]["claim_ids"].append(claim["id"])
                    claim["passage_ids"].append(passage_id)
                    locations.append((row["repository"], row["path"], row["line_start"], row["line_end"]))
                    if len(claim["passage_ids"]) >= limit_per_claim:
                        break
        context["passages"] = list(selected.values())
        context["snapshot_sha256"] = _fingerprint(context)
        return context
