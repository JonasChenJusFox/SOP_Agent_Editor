"""Local knowledge defaults and host-side freshness checks.

SQLite is imported lazily. Pure cloud code can report unavailable local sources
without opening an index, reading repositories, or uploading private data.
"""

import re
import tomllib
from pathlib import Path

from .review import fingerprint, paragraph_spans

DEFAULT_NAMES = ("nearbite", "aitherpath")
KNOWLEDGE_KEYS = {"enabled", "index", "auto_refresh", "limit_per_claim", "sources"}


def project_root(start=None):
    start = Path(start or Path.cwd()).resolve()
    for parent in (start, *start.parents):
        path = parent / "pyproject.toml"
        if path.is_file():
            try:
                if tomllib.loads(path.read_text()).get("project", {}).get("name") == "admissions-editor-agent":
                    return parent
            except (OSError, ValueError):
                pass
    source_root = Path(__file__).resolve().parents[2]
    return source_root if (source_root / "knowledge.toml").is_file() else start


def validate_settings(value, base):
    if not isinstance(value, dict) or set(value) - KNOWLEDGE_KEYS:
        raise ValueError("Unknown knowledge configuration field.")
    value = dict(value)
    for key in ("enabled", "auto_refresh"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"knowledge.{key} must be a boolean.")
    if "limit_per_claim" in value and (isinstance(value["limit_per_claim"], bool) or
            not isinstance(value["limit_per_claim"], int) or not 1 <= value["limit_per_claim"] <= 8):
        raise ValueError("knowledge.limit_per_claim must be 1..8.")
    if "index" in value:
        if not isinstance(value["index"], str) or not value["index"].strip():
            raise ValueError("knowledge.index must be a local file path.")
        value["index"] = str((base / value["index"]).resolve())
    if "sources" in value:
        if not isinstance(value["sources"], list) or len(value["sources"]) > 16:
            raise ValueError("knowledge.sources must be a list of at most 16 repositories.")
        sources, names = [], set()
        for source in value["sources"]:
            if not isinstance(source, dict) or set(source) - {"name", "path", "required"}:
                raise ValueError("A knowledge source accepts name, path and required only.")
            name, path = source.get("name"), source.get("path")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name) or name in names:
                raise ValueError("Knowledge source names must be unique local identifiers.")
            if not isinstance(path, str) or not path.strip() or "\0" in path:
                raise ValueError("Knowledge sources require local directory paths.")
            if not isinstance(source.get("required", True), bool):
                raise ValueError("knowledge source required must be boolean.")
            names.add(name)
            sources.append({"name": name, "path": str((base / path).resolve()), "required": source.get("required", True)})
        value["sources"] = sources
    return value


def settings(config=None, repos=None, knowledge_config=None, disabled=False, root=None):
    root = project_root(root)
    value = {"enabled": True, "index": str(root / "private/knowledge/index.sqlite3"),
             "auto_refresh": True, "limit_per_claim": 3,
             "sources": [{"name": name, "path": str(root / "private/knowledge/repos" / name), "required": True}
                         for name in DEFAULT_NAMES]}
    path = Path(knowledge_config).resolve() if knowledge_config else root / "knowledge.toml"
    if path.exists():
        data = tomllib.loads(path.read_text())
        if set(data) != {"knowledge"}:
            raise ValueError("Knowledge config must contain only [knowledge].")
        value.update(validate_settings(data["knowledge"], path.parent))
    elif knowledge_config:
        raise ValueError("Explicit knowledge configuration file is missing.")
    value.update(validate_settings((config or {}).get("knowledge", {}), root))
    if repos is not None:
        sources = []
        for entry in repos:
            name, separator, path = str(entry).partition("=")
            if not separator:
                path, name = name, Path(name).name
            sources.append({"name": name, "path": path, "required": True})
        value.update(validate_settings({"sources": sources}, root))
    private_root = (root / "private/knowledge").resolve()
    if not private_root.is_relative_to(root):
        raise ValueError("private/knowledge must remain inside this project, not an escaping symlink.")
    index = Path(value["index"]).resolve()
    if not index.is_relative_to(private_root) or index == private_root:
        raise ValueError("Keep the knowledge index inside this project's private/knowledge/ directory.")
    for source in value["sources"]:
        if index.is_relative_to(Path(source["path"])):
            raise ValueError("The index must not be stored inside a source repository.")
    value["enabled"] = value["enabled"] and not disabled
    return value


def unavailable_knowledge_context(reason, disabled=False):
    status = "disabled" if disabled else "missing"
    sources = [{"name": name, "path": f"private/knowledge/repos/{name}", "status": status,
                "commit_sha": None, "snapshot_hash": None, "file_count": 0, "passage_count": 0,
                "changed": False} for name in DEFAULT_NAMES]
    context = {"schema_version": "knowledge-context-1", "status": status, "sources": sources,
            "claims": [], "passages": [], "diagnostics": [reason],
            }
    context["snapshot_sha256"] = fingerprint(context)
    return context


def essay_claims(draft, source_names=DEFAULT_NAMES):
    from .technical import extract_technical_context
    context = extract_technical_context({"draft": draft, "evidence": {}})
    claims = [{"id": claim["id"], "text": claim["quote"]}
              for claim in context["claims"] if claim["source_type"] == "draft"]
    covered = {claim["text"] for claim in claims}
    # Include nontechnical experience/outcome claims as well as recognized terms.
    for paragraph_id, (_, text) in paragraph_spans(draft).items():
        for index, match in enumerate(re.finditer(r"[^.!?\n]+(?:[.!?](?=\s|$)|$)", text), 1):
            quote = match.group().strip()
            if quote and quote not in covered:
                claims.append({"id": f"ESSAY-{paragraph_id}-{index}", "text": quote})
                covered.add(quote)
    paragraphs = [text for _, text in paragraph_spans(draft).values()]
    for claim in claims:
        paragraph = next((text for text in paragraphs if claim["text"] in text), "")
        compact = re.sub(r"[^a-z0-9]", "", paragraph.casefold())
        named = [name for name in source_names if re.sub(r"[^a-z0-9]", "", name.casefold()) in compact]
        if len(named) == 1 and named[0].casefold() not in claim["text"].casefold():
            claim["query"] = claim["text"] + " " + named[0]
    return claims


def prepare_knowledge(draft, config=None, repos=None, knowledge_config=None, disabled=False, root=None):
    selected = settings(config, repos, knowledge_config, disabled, root)
    if not selected["enabled"]:
        return unavailable_knowledge_context("Repository knowledge was explicitly disabled for this run.", disabled=True)
    from .knowledge import KnowledgeIndex
    index = KnowledgeIndex(Path(selected["index"]), selected["sources"], auto_refresh=selected["auto_refresh"])
    return index.retrieve(essay_claims(draft, [source["name"] for source in selected["sources"]]),
                          limit_per_claim=selected["limit_per_claim"])


def assert_knowledge_current(context, config=None, repos=None, knowledge_config=None, disabled=False, root=None):
    if not context or context.get("status") == "disabled":
        return
    used = {passage["repository"] for passage in context.get("passages", [])}
    if not used:
        return
    selected = settings(config, repos, knowledge_config, disabled, root)
    if not selected["enabled"]:
        raise ValueError("Knowledge was disabled after planning; prepare a new plan.")
    from .knowledge import KnowledgeIndex
    from .review import validate_knowledge_context
    validate_knowledge_context(context)
    KnowledgeIndex(Path(selected["index"]), selected["sources"], auto_refresh=True).validate_context(context)
