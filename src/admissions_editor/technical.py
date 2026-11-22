"""Offline technical terminology inventory and read-only revision checks.

The inventory records what the supplied passages say. It is not a knowledge base,
proof of factual truth, or an exhaustive semantic parser. A glossary explains
terminology; its definitions are never evidence for an applicant's work.
"""

import copy
import difflib
import json
import re

from .review import fingerprint, paragraph_spans, sha256

TECHNICAL_VERSION = "technical-context-2"
MAX_GLOSSARY_BYTES = 32 * 1024

# Canonical groups deliberately keep neighboring concepts separate. This small
# vocabulary is an offline starting point, supplemented by supplied terminology.
_VOCABULARY = {
    "method": {
        "retrieval": ["retrieval", "retriever", "retrieving", "retrieved"],
        "ranking": ["ranking", "ranker", "reranking", "re-ranking", "re-ranker"],
        "embeddings": ["embedding", "embeddings", "vector embeddings"],
        "fine-tuning": ["fine-tuning", "fine tuning", "fine-tuned", "fine tuned", "fine-tune"],
        "semantic search": ["semantic search"], "BM25": ["BM25"],
        "RAG": ["RAG", "retrieval-augmented generation"],
        "gradient descent": ["gradient descent"], "cross-validation": ["cross-validation", "cross validation"],
        "k-means": ["k-means"], "A/B testing": ["A/B testing", "A/B test"],
        "distributed systems": ["distributed systems"],
    },
    "model": {
        "model": ["model", "models", "underlying model"],
        "transformer": ["transformer", "transformers"], "BERT": ["BERT"],
        "LLM": ["LLM", "LLMs", "large language model", "large language models"],
        "neural network": ["neural network", "neural networks"],
        "logistic regression": ["logistic regression"], "random forest": ["random forest"],
        "XGBoost": ["XGBoost"], "GPT": ["GPT", "GPT-4", "GPT-4o"],
    },
    "metric": {
        "precision": ["precision"], "recall": ["recall"], "accuracy": ["accuracy"],
        "F1": ["F1", "F1-score", "F1 score"], "AUROC": ["AUROC", "ROC-AUC"],
        "AUC": ["AUC"], "NDCG": ["NDCG"], "MRR": ["MRR"],
        "RMSE": ["RMSE"], "MAE": ["MAE"], "latency": ["latency"],
        "throughput": ["throughput"], "p95": ["p95"], "p99": ["p99"],
    },
    "data_source": {
        "training data": ["training data", "training set"],
        "validation data": ["validation data", "validation set"],
        "test data": ["test data", "test set", "held-out test set", "held-out test data"],
        "dataset": ["dataset", "datasets"], "logs": ["logs", "log data"],
        "saved messages": ["saved messages"], "telemetry": ["telemetry"],
        "MovieLens": ["MovieLens"], "ImageNet": ["ImageNet"], "CIFAR-10": ["CIFAR-10"],
        "OpenStreetMap": ["OpenStreetMap"], "public data": ["public data", "public dataset"],
        "private data": ["private data", "private dataset"],
    },
    "library": {
        "PyTorch": ["PyTorch"], "TensorFlow": ["TensorFlow"],
        "scikit-learn": ["scikit-learn", "sklearn"], "pandas": ["pandas"],
        "NumPy": ["NumPy"], "FAISS": ["FAISS"], "FastAPI": ["FastAPI"],
        "React": ["React"], "Spark": ["Spark", "PySpark"],
        "Hugging Face": ["Hugging Face", "HuggingFace"],
    },
    "system_component": {
        "interface": ["interface", "user interface", "UI", "front end", "front-end", "frontend",
                      "user-facing app", "user facing app", "user-facing application", "user facing application",
                      "user-facing web app", "user facing web app", "user-facing web application", "user facing web application"],
        "dashboard": ["dashboard"], "backend": ["backend", "back-end", "back end"],
        "parser": ["parser"], "replay tool": ["replay tool"],
        "API": ["API", "APIs"], "cache": ["cache", "caching layer"],
        "index": ["index", "vector index"], "vector database": ["vector database"],
        "feature pipeline": ["feature pipeline"], "serving pipeline": ["serving pipeline"],
        "database": ["database"], "PostgreSQL": ["PostgreSQL", "Postgres"],
        "SQL": ["SQL"], "message queue": ["message queue"],
    },
}

_DISTINCTIONS = [
    {"id": "retrieval-vs-ranking", "concepts": ["retrieval", "ranking"],
     "reason": "Retrieving candidates and ordering candidates are different operations."},
    {"id": "embeddings-vs-fine-tuning", "concepts": ["embeddings", "fine-tuning"],
     "reason": "Using or producing embeddings does not establish that model weights were fine-tuned."},
    {"id": "interface-vs-model-development", "concepts": ["interface", "model"],
     "reason": "Building an interface does not establish development or training of the underlying model."},
    {"id": "training-validation-test", "concepts": ["training data", "validation data", "test data"],
     "reason": "Training, validation and held-out test data are distinct evaluation scopes."},
]


def _plain(value, limit, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value.encode()) > limit:
        raise ValueError(f"{label} must be nonempty plain text within its size limit.")
    return value.strip()


def _key(value):
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _known_canonical(value):
    key = _key(value)
    for values in _VOCABULARY.values():
        for canonical, aliases in values.items():
            if key in {_key(a) for a in aliases}:
                return canonical
    return None


def normalize_glossary(value=None):
    """Validate optional applicant terminology; do not confer factual authority."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError("Glossary must be a list of at most 128 entries.")
    result, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"term", "definition", "aliases"}:
            raise ValueError("Glossary entries support term, definition and aliases only.")
        term = _plain(entry.get("term"), 120, "Glossary term")
        definition = _plain(entry.get("definition"), 4000, "Glossary definition")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 16:
            raise ValueError("Glossary aliases must be a list of at most 16 terms.")
        aliases = [_plain(a, 120, "Glossary alias") for a in aliases]
        keys = [_key(a) for a in [term, *aliases]]
        if len(set(keys)) != len(keys) or seen.intersection(keys):
            raise ValueError("Glossary terms and aliases must be unique, ignoring case and whitespace.")
        canonicals = {_known_canonical(a) for a in [term, *aliases]} - {None}
        if any(len(canonicals.intersection(d["concepts"])) > 1 for d in _DISTINCTIONS):
            raise ValueError("Glossary aliases cannot collapse distinct technical operations or data scopes.")
        seen.update(keys)
        result.append({"term": term, "definition": definition, "aliases": aliases})
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_GLOSSARY_BYTES:
        raise ValueError("Glossary exceeds the 32 KB limit.")
    return result


def _pattern(term):
    return re.compile(r"(?<![\w])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![\w])", re.I)


def _academic_abbreviation(text, start, end):
    """Recognize degree-label usage without ignoring project acronyms globally."""
    if text[start:end] not in {"SM", "MS", "MA", "MBA", "BA", "BS", "MENG", "MPH", "PHD", "DS"}:
        return False
    before, after = text[max(0, start - 90):start], text[end:end + 110]
    discipline = r"(?:data\s+science|computer\s+science|statistics|mathematics|engineering|business\s+administration|public\s+health|economics)"
    if re.match(r"\s+(?:(?:degree|program|programme)\s+)?(?:(?:in|of)\s+)?" + discipline + r"\b", after, re.I):
        return True
    admission = bool(re.search(r"\b(?:appl(?:y|ying|ication)|admission|master'?s?|graduate|pursu\w*|enroll\w*|degree|university)\b", before, re.I))
    return admission and bool(re.match(r"\s*(?:degree|program|programme|application|admission|student|at\b|in\b)", after, re.I))


def _matches(text, glossary):
    found = []
    for category, groups in _VOCABULARY.items():
        for canonical, aliases in groups.items():
            for alias in sorted(aliases, key=lambda a: (-len(a), a)):
                for match in _pattern(alias).finditer(text):
                    found.append((match.start(), match.end(), category, canonical, None))
    for entry in glossary:
        for alias in [entry["term"], *entry["aliases"]]:
            for match in _pattern(alias).finditer(text):
                found.append((match.start(), match.end(), "project_term", entry["term"], entry["term"]))
    # Supplied project identifiers are retained without inventing a definition.
    for match in re.finditer(r"`([^`\n]{1,120})`", text):
        found.append((match.start(1), match.end(1), "project_term", match.group(1), None))
    for match in re.finditer(r"\b[A-Z][A-Z0-9_-]{1,15}\b", text):
        if match.group() not in {"I", "SOP", "TODO", "TBD", "PS"} and not _academic_abbreviation(text, match.start(), match.end()):
            found.append((match.start(), match.end(), "technical_identifier", match.group(), None))
    for match in re.finditer(r"\b(?:library|framework|package|dataset|component)\s+([A-Z][A-Za-z0-9_-]{1,79})\b", text):
        found.append((match.start(1), match.end(1), "project_term", match.group(1), None))
    # Prefer the longest spelling within one canonical concept; e.g. do not
    # extract a second 'model' nested inside 'underlying model'. Keep glossary
    # matches even when they explain a built-in term.
    chosen = []
    for item in sorted(set(found), key=lambda m: (m[0], -(m[1] - m[0]), m[2], m[3], m[4] is None, m[4] or "")):
        start, end, category, canonical, explanation = item
        if any(start >= a and end <= b and ((category == c and canonical == d) or
                   (category == "technical_identifier" and c != "project_term")) for a, b, c, d, _ in chosen):
            continue
        chosen.append(item)
    return sorted(chosen, key=lambda m: (m[0], m[1], m[2], m[3]))


def _sentences(text):
    start = 0
    for boundary in [*re.finditer(r"[.!?](?=\s+[A-Z\"“‘]|\s*$)|\n+", text), None]:
        end = boundary.end() if boundary else len(text)
        raw = text[start:end]
        quote = raw.strip()
        if quote:
            left = start + len(raw) - len(raw.lstrip())
            yield left, left + len(quote), quote
        start = end


def _contribution(text):
    return bool(re.search(r"\b(?:I|my|we|our|author|applicant|team|teammates?)\b", text, re.I) and
                re.search(r"\b(?:built|build|develop\w*|implement\w*|design\w*|train\w*|fine[ -]tun\w*|"
                          r"integrat\w*|wrote|write|owned|own|evaluat\w*|tested|test|used|use|led|creat\w*|handl\w*)\b", text, re.I))


def extract_technical_context(snapshot, glossary=None, confirmed_facts=None):
    """Extract exact, stable source-bound terms and claims before writing."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("draft"), str) or not isinstance(snapshot.get("evidence", {}), dict):
        raise ValueError("Technical context needs draft text and supplied evidence.")
    glossary = normalize_glossary(glossary)
    sources = [{"source_type": "draft", "source_id": paragraph_id, "text": text,
                "sha256": sha256(text.encode())} for paragraph_id, (_, text) in paragraph_spans(snapshot["draft"]).items()]
    for source_id, text in sorted(snapshot.get("evidence", {}).items()):
        if not isinstance(source_id, str) or not isinstance(text, str):
            raise ValueError("Evidence IDs and texts must be strings.")
        sources.append({"source_type": "evidence", "source_id": source_id, "text": text, "sha256": sha256(text.encode())})
    if confirmed_facts is not None and not isinstance(confirmed_facts, list):
        raise ValueError("Confirmed technical facts must be a list.")
    fact_ids = set()
    for fact in confirmed_facts or []:
        if not isinstance(fact, dict):
            raise ValueError("Confirmed technical facts must be records.")
        if fact.get("confirmed") is not True:
            continue  # Unconfirmed prose is neither retained nor sent to a writer.
        fact_id, text = fact.get("id"), fact.get("text")
        if not isinstance(fact_id, str) or not fact_id.strip() or fact_id in fact_ids:
            raise ValueError("Confirmed technical fact IDs must be nonempty and unique.")
        text = _plain(text, 128 * 1024, "Confirmed technical fact")
        fact_ids.add(fact_id)
        sources.append({"source_type": "confirmed_fact", "source_id": fact_id, "text": text,
                        "sha256": sha256(text.encode()), "authority": "author_confirmation"})
    terms, claims = [], []
    for source in sources:
        local_terms = []
        for start, end, category, canonical, glossary_term in _matches(source["text"], glossary):
            term = {"term": source["text"][start:end], "canonical": canonical, "category": category,
                    "source_type": source["source_type"], "source_id": source["source_id"],
                    "quote": source["text"][start:end], "start": start, "end": end, "glossary_term": glossary_term}
            term["id"] = "TERM-" + fingerprint(term)[:16]
            local_terms.append(term)
        terms.extend(local_terms)
        if not local_terms:
            continue
        for start, end, quote in _sentences(source["text"]):
            contained = [term for term in local_terms if term["start"] >= start and term["end"] <= end]
            contribution = _contribution(quote)
            if not contained and not contribution:
                continue
            categories = sorted({term["category"] for term in contained} | ({"applicant_contribution"} if contribution else set()))
            claim = {"source_type": source["source_type"], "source_id": source["source_id"], "quote": quote,
                     "start": start, "end": end, "term_ids": [term["id"] for term in contained],
                     "categories": categories, "applicant_contribution": contribution}
            # Classification and glossary annotations can grow without changing
            # the identity of the same frozen source passage. The enclosing
            # revision plan binds the complete context and its classifications.
            claim["id"] = "TECH-" + fingerprint({key: claim[key] for key in
                ("source_type", "source_id", "quote", "start", "end")})[:16]
            claims.append(claim)
    return {"schema_version": TECHNICAL_VERSION, "glossary": glossary, "terms": terms, "claims": claims,
            "sources": sources, "distinctions": copy.deepcopy(_DISTINCTIONS),
            "terminology_context": {"requirements": snapshot.get("requirements", ""),
                "scope": "Application wording only; not evidence for applicant contributions or metrics."},
            "check_required_claim_ids": [c["id"] for c in claims if c["source_type"] == "draft"],
            "scope": "Offline heuristic extraction from draft, evidence and explicitly confirmed applicant facts; author confirmation is not independent verification. Requirements and glossary are terminology guidance only."}


def _issue(kind, claim, final_quote, reason):
    return {"kind": kind, "claim_id": claim.get("id") if claim else None,
            "original_quote": claim.get("quote") if claim else None, "final_quote": final_quote,
            "source_type": claim.get("source_type") if claim else None,
            "source_id": claim.get("source_id") if claim else None,
            "source_quote": claim.get("quote") if claim else None, "reason": reason}


def validate_writing_technical_flags(context, essay, flags):
    """A writer flag must retain the original technical wording for verification."""
    if not isinstance(flags, list):
        return [_issue("invalid_flag", None, None, "technical_flags must be a list.")]
    claims = {c["id"]: c for c in context["claims"] if c["source_type"] == "draft"}
    issues, seen = [], set()
    for flag in flags:
        if not isinstance(flag, dict) or set(flag) != {"claim_id", "reason"}:
            issues.append(_issue("invalid_flag", None, None, "Technical flags require claim_id and reason."))
            continue
        claim = claims.get(flag["claim_id"])
        if claim is None or flag["claim_id"] in seen or not isinstance(flag["reason"], str) or not flag["reason"].strip():
            issues.append(_issue("invalid_flag", claim, None, "Flag needs a known, unique draft claim ID and a reason."))
            continue
        seen.add(flag["claim_id"])
        if claim["quote"] not in essay:
            issues.append(_issue("flagged_wording_changed", claim, None,
                                 "The writer flagged a potential technical/contribution change but failed to retain the original wording."))
    return issues


def _canonical(text, glossary):
    return {m[3] for m in _matches(text, glossary)}


def _aligned_quote(original, final, check, glossary=()):
    if check and isinstance(check.get("final_quote"), str) and check["final_quote"] in final:
        return check["final_quote"]
    if original in final:
        return original
    candidates = [quote for _, quote in paragraph_spans(final).values()]
    words = set(re.findall(r"\w+", original.casefold()))
    if not candidates:
        return None
    def score(candidate):
        other = set(re.findall(r"\w+", candidate.casefold()))
        return len(words.intersection(other)) / max(len(words), 1) + difflib.SequenceMatcher(None, original.casefold(), candidate.casefold()).ratio() * .2
    best = max(candidates, key=score)
    if score(best) >= .24:
        return best
    # Specific lexical aliases can leave few shared words ("developed a
    # user-facing application" -> "built the interface"). Use that fallback
    # only when a nonempty actor/action/component binding is unchanged.
    original_concepts, original_bindings = _canonical(original, glossary), _actor_bindings(original, glossary)
    if original_concepts and original_bindings:
        return next((candidate for candidate in candidates if
            _canonical(candidate, glossary) == original_concepts and
            _actor_bindings(candidate, glossary) == original_bindings), None)
    return None


def _metric_values(text):
    values = set()
    metrics = [m for m in _matches(text, []) if m[2] == "metric"]
    for index, (start, end, _, canonical, _) in enumerate(metrics):
        stop = metrics[index + 1][0] if index + 1 < len(metrics) else len(text)
        suffix = text[end:stop]
        topk = re.match(r"@\d+", suffix)
        if topk:
            canonical += topk.group()
            suffix = suffix[topk.end():]
        # Only the metric's local clause is used; a later method or unrelated
        # sentence is not evidence for a metric/value association.
        suffix = re.split(r"[;!?\n]|\.(?=\s+[A-Z]|$)", suffix, 1)[0][:100]
        for value in re.findall(r"(?<![\w])\d+(?:[.,]\d+)*%?", suffix)[:2]:
            values.add((canonical, value.replace(",", "")))
    return values


def _personal_model_development(text):
    for match in re.finditer(r"\b(?:I|the author|the applicant)\b(.{0,100}?)(?:[.;!?]|$)", text, re.I):
        clause = match.group(1)
        if re.search(r"\b(?:not|never|without|didn't|wasn't)\b", clause, re.I):
            continue
        for operation in re.finditer(r"\b(?:develop\w*|train\w*|fine[ -]tun\w*|creat\w*|built|build)\b(.{0,65}?)\b(?:underlying model|models?|BERT|LLM|transformer|neural network)\b", clause, re.I):
            # 'I built the interface for the model' describes an interface, not
            # model development. Do not mistake its later prepositional object
            # for the applicant's direct contribution.
            if _canonical(operation.group(1), []).intersection({"interface", "dashboard", "API", "parser", "replay tool", "cache", "feature pipeline", "serving pipeline"}):
                continue
            if re.match(r"(?:['’]s\s+|\s+)(?:interface|dashboard|API)\b", clause[operation.end():], re.I):
                continue
            return True
    return False


def _actor_bindings(text, glossary):
    """Conservative explicit actor/operation/component bindings, not entailment."""
    bindings = set()
    actors = list(re.finditer(r"\bI\b|\b(?:the\s+)?(?:author|applicant)\b|\bmy\s+role\b|"
                             r"\bmy\s+(?:teammates?|colleagues?)\b|\b(?:research\s+)?team\b|\bwe\b", text, re.I))
    for index, actor in enumerate(actors):
        stop = actors[index + 1].start() if index + 1 < len(actors) else len(text)
        segment = re.split(r"[;.!?](?=\s|$)|\n", text[actor.end():stop], 1)[0]
        role = "personal" if re.fullmatch(r"I|(?:the\s+)?(?:author|applicant)|my\s+role", actor.group(), re.I) else "team"
        if re.fullmatch(r"I", actor.group(), re.I):
            # A clear affirmative participial clause may put the applicant's
            # contribution before its subject: "While building the interface,
            # I learned ...". Keep this narrow: component actions only, no
            # intervening actor, hypothetical condition, negation or future.
            prefix = re.split(r"[;.!?](?=\s|$)|\n", text[:actor.start()])[-1]
            prefix_start = actor.start() - len(prefix)
            # Author answer fields can carry a neutral label before the actual
            # sentence. Retain that exact label in all stored citations; only
            # the grammar parser skips this explicit, bounded list. Attribution
            # labels such as "teammate:" are deliberately not stripped.
            prefix = re.sub(r"^\s*(?:参考|回答|答复|补充|说明|备注|作者确认|作者补充|Answer|Response|Reference)\s*[:：]\s*",
                            "", prefix, count=1, flags=re.I)
            previous_actor = actors[index - 1] if index else None
            clause = re.fullmatch(r"\s*(?:when|while|by|after|through|in)\s+"
                                  r"((?:building|developing|implementing|designing|creating|writing|integrating|handling)\b.{0,500}?)\s*[,\:]?\s*", prefix, re.I)
            uncertain = r"\b(?:if|unless|before|without|instead|not|never|would|could|might|may|will|shall|can|plan\w*|intend\w*|hope\w*|propos\w*|wish\w*|attempt\w*)\b"
            if clause and not (previous_actor and previous_actor.start() >= prefix_start) and not \
                    re.search(uncertain, prefix, re.I) and not re.search(uncertain, " ".join(segment.split()[:8]), re.I):
                bindings.update((bound_role, operation, canonical) for bound_role, operation, canonical in
                                _actor_bindings("I " + clause.group(1).rstrip(" ,:"), glossary)
                                if canonical in _VOCABULARY["system_component"] and operation in {"development", "usage", "responsibility"})
        actions = list(re.finditer(r"\b(?:build\w*|built|develop\w*|implement\w*|design\w*|creat\w*|writ\w*|wrote|own\w*|"
                                   r"train\w*|fine[ -]tun\w*|integrat\w*|us(?:e|ed|ing)|evaluat\w*|test\w*|measur\w*|handl\w*)\b", segment, re.I))
        for action_index, action in enumerate(actions):
            prefix = segment[:action.start()]
            if re.search(r"\b(?:not|never|without|didn't)\b", " ".join(prefix.split()[-4:]), re.I):
                continue
            verb = action.group().casefold()
            operation = "training" if re.match(r"train|fine", verb) else "usage" if re.match(r"integrat|us", verb) else "evaluation" if re.match(r"evaluat|test|measur", verb) else "responsibility" if re.match(r"handl", verb) else "development"
            action_stop = actions[action_index + 1].start() if action_index + 1 < len(actions) else len(segment)
            tail = segment[action.end():action_stop]
            # A method/library in a prepositional phrase is not thereby the
            # direct object an applicant built. 'built an interface for BERT'
            # does not establish that the applicant built BERT.
            direct = re.split(r"\b(?:for|using|with|on|from|through|that|which)\b", tail, 1, flags=re.I)[0]
            # Coordinate objects share this action, whereas a following action
            # starts its own binding: built backend and interface; built the
            # interface and trained the model. Do not transfer the first verb
            # across that second clause.
            for object_text in re.split(r",|\band\b", direct, flags=re.I):
                concepts = _canonical(object_text, glossary)
                matches = _matches(object_text, glossary)
                if operation == "development" and matches and any(
                        canonical == "interface" and not object_text[end:].strip(" .,:;!?")
                        for _, end, _, canonical, _ in matches):
                    # A "ranking model interface" is an interface, not personal
                    # development of its ranking method, model, or library.
                    concepts = {"interface"}
                if operation == "responsibility":
                    # Handling a component is responsibility, not a declaration
                    # that the applicant built or trained its underlying model.
                    concepts.intersection_update(_VOCABULARY["system_component"])
                for canonical in concepts:
                    bindings.add((role, operation, canonical))
    return bindings


def _negated_concepts(text, glossary):
    result = set()
    for start, _, _, canonical, _ in _matches(text, glossary):
        prefix = re.split(r"[.;!?]|\b(?:but|whereas|while)\b", text[:start], flags=re.I)[-1]
        prefix = " ".join(prefix.split()[-7:])
        if re.search(r"\b(?:not|never|without|no|didn't|wasn't)\b", prefix, re.I):
            result.add(canonical)
    return result


def check_technical_accuracy(context, final, checks=None, *, used_fact_ids=None):
    """Read-only technical check; model assertions cannot bypass local checks.

    ``checks=None`` performs the offline baseline. Providing model checks requires
    exactly one result for every extracted draft claim and validates citations.
    The model and applicant remain responsible for unrecognized semantic drift.
    """
    if not isinstance(final, str):
        raise ValueError("Technical check requires candidate text.")
    glossary = context["glossary"]
    required = set(context["check_required_claim_ids"])
    claims = {c["id"]: c for c in context["claims"]}
    active_fact_ids = None if used_fact_ids is None else set(used_fact_ids)
    active_sources = [source for source in context["sources"] if source["source_type"] != "confirmed_fact" or
                      active_fact_ids is None or source["source_id"] in active_fact_ids]
    source_lookup = {(s["source_type"], s["source_id"]): s["text"] for s in active_sources}
    def omitted_fact(predicate):
        return next((claim for claim in context["claims"] if claim["source_type"] == "confirmed_fact" and
                     active_fact_ids is not None and claim["source_id"] not in active_fact_ids and predicate(claim["quote"])), None)
    issues, by_claim = [], {}
    if checks is not None:
        if not isinstance(checks, list):
            issues.append(_issue("invalid_checks", None, None, "technical_checks must be a list."))
            checks = []
        for check in checks:
            if not isinstance(check, dict):
                issues.append(_issue("invalid_check", None, None, "Invalid technical check record."))
                continue
            claim = claims.get(check.get("claim_id"))
            if claim is None or check.get("claim_id") in by_claim:
                issues.append(_issue("invalid_check", claim, check.get("final_quote"), "Technical check ID is unknown or duplicated."))
                continue
            by_claim[claim["id"]] = check
            final_quote = check.get("final_quote")
            status = check.get("status")
            source = source_lookup.get((check.get("source_type"), check.get("source_id")), "")
            if check.get("source_type") == "confirmed_fact" and active_fact_ids is not None and check.get("source_id") not in active_fact_ids:
                issues.append(_issue("unreported_confirmed_fact", claim, final_quote,
                                     "Technical verification cites a confirmed fact that no writing stage reported in used_fact_ids."))
            if check.get("original_quote") != claim["quote"] or not isinstance(check.get("source_quote"), str) or not check["source_quote"].strip() or check["source_quote"] not in source or \
                    not isinstance(check.get("reason"), str) or not check["reason"].strip() or \
                    (final_quote is not None and (not isinstance(final_quote, str) or not final_quote.strip() or final_quote not in final)):
                issues.append(_issue("invalid_check", claim, final_quote, "Technical check quotations must exactly match the frozen source and candidate passages."))
            if status == "not_used" and claim["source_type"] == "evidence" and final_quote is None:
                continue
            if status not in {"preserved", "supported_change"} or final_quote is None:
                issues.append(_issue(status or "invalid_status", claim, final_quote, check.get("reason") or "Technical meaning is unresolved."))
            elif status == "preserved" and (claim["quote"] not in final or claim["quote"] not in final_quote):
                issues.append(_issue("invalid_preservation", claim, final_quote, "A preserved technical claim must retain the exact original wording."))
        for claim_id in sorted(required - by_claim.keys()):
            issues.append(_issue("missing_check", claims[claim_id], None, "Read-only verification did not cover this draft technical claim."))

    allowed = set().union(*(_canonical(s["text"], glossary) for s in active_sources))
    final_terms = _canonical(final, glossary)
    for canonical in sorted(final_terms - allowed):
        occurrence = next((q for _, _, q in _sentences(final) if canonical in _canonical(q, glossary)), final)
        anchor = next((c for c in context["claims"] if c["source_type"] == "draft"), None)
        if anchor is None and context["sources"]:
            source = context["sources"][0]
            anchor = {"source_type": source["source_type"], "source_id": source["source_id"], "quote": source["text"]}
        omitted = omitted_fact(lambda quote: canonical in _canonical(quote, glossary))
        if omitted is not None:
            issues.append(_issue("unreported_confirmed_fact", omitted, occurrence,
                                 f"Technical detail {canonical!r} needs confirmed fact {omitted['source_id']} reported in used_fact_ids."))
        else:
            issues.append(_issue("unsupported_term", anchor, occurrence,
                                 f"Technical term {canonical!r} is absent from the draft, supplied evidence and reported confirmed facts; a glossary definition is not factual support."))
    if _personal_model_development(final) and not any(_personal_model_development(s["text"]) for s in active_sources):
        claim = next((c for c in context["claims"] if c["source_type"] == "draft" and c["applicant_contribution"]), None)
        omitted = omitted_fact(_personal_model_development)
        issues.append(_issue("unreported_confirmed_fact" if omitted else "contribution_drift", omitted or claim, final,
                             f"Report confirmed fact {omitted['source_id']} in used_fact_ids before using its contribution claim." if omitted else
                             "The candidate attributes model development/training to the applicant without that contribution in supplied passages."))
    source_metrics = set().union(*(_metric_values(s["text"]) for s in active_sources))
    for metric, value in sorted(_metric_values(final) - source_metrics):
        claim = next((c for c in context["claims"] if c["source_type"] == "draft" and "metric" in c["categories"]), None)
        omitted = omitted_fact(lambda quote: (metric, value) in _metric_values(quote))
        issues.append(_issue("unreported_confirmed_fact" if omitted else "metric_drift", omitted or claim, final,
                             f"Report confirmed fact {omitted['source_id']} in used_fact_ids before using its metric/value association." if omitted else
                             f"Metric/value association {metric}={value} is not present in supplied passages; verify the metric, value and evaluation scope."))
    source_bindings = set().union(*(_actor_bindings(s["text"], glossary) for s in active_sources))
    for actor, operation, canonical in sorted(_actor_bindings(final, glossary) - source_bindings):
        # Usage/evaluation may be newly described with evidence, but upgrading
        # someone else's development/training to the applicant is material.
        if actor == "personal" and operation in {"development", "training"}:
            anchor = next((c for c in context["claims"] if c["source_type"] == "draft" and c["applicant_contribution"]), None)
            omitted = omitted_fact(lambda quote: (actor, operation, canonical) in _actor_bindings(quote, glossary))
            issues.append(_issue("unreported_confirmed_fact" if omitted else "contribution_drift", omitted or anchor, final,
                                 f"Report confirmed fact {omitted['source_id']} in used_fact_ids before using this contribution." if omitted else
                                 f"The candidate newly attributes {operation} of {canonical!r} to the applicant; retain the original contribution wording and verify."))
    for claim_id in context["check_required_claim_ids"]:
        claim = claims[claim_id]
        original = claim["quote"]
        if original in final:
            continue
        candidate = _aligned_quote(original, final, by_claim.get(claim_id), glossary)
        if candidate is None:
            issues.append(_issue("ambiguous", claim, None, "The technical claim cannot be aligned; retain the original wording and verify before accepting."))
            continue
        before = _canonical(original, glossary)
        after = _canonical(candidate, glossary)
        for distinction in _DISTINCTIONS:
            group = set(distinction["concepts"])
            lost = before.intersection(group) - after
            introduced = after.intersection(group) - before
            if lost and introduced:
                issues.append(_issue("altered", claim, candidate, distinction["reason"] + " Retain the original wording and verify the proposed change."))
        lost_negations = _negated_concepts(original, glossary) - _negated_concepts(candidate, glossary)
        if lost_negations.intersection(after):
            issues.append(_issue("negation_drift", claim, candidate,
                                 "A technical limitation or negated operation may have become a positive claim; retain the original wording and verify."))
        critical = {t["canonical"] for t in context["terms"] if t["id"] in claim["term_ids"] and
                    t["category"] in {"method", "metric", "library", "data_source", "project_term"}}
        missing = critical - final_terms
        if missing:
            issues.append(_issue("ambiguous", claim, candidate,
                                 "Technical terminology was removed without a verified equivalence: " + ", ".join(sorted(missing)) + ". Retain the original wording and verify."))
    unique = {fingerprint(issue): issue for issue in issues}
    issues = list(unique.values())
    return {"passed": not issues, "issues": issues, "checks": copy.deepcopy(checks) if checks is not None else [],
            "required_claim_count": len(required),
            "scope": "Read-only offline lexical and source checks plus supplied model judgments; not exhaustive semantic verification or factual proof."}
