"""Explicit TOML loading; secrets remain in the process environment."""

import os
import tomllib
from pathlib import Path

from .providers import ROLE_NAMES, FixtureProvider, OpenAIProvider, RoutedProvider, TaskRoutedProvider

REVISION_SECTIONS = {"sop_revise": "reviser", "polish": "polisher", "humanize": "humanizer", "verify_revision": "revision_verifier"}


def load_config(path):
    if path is None:
        return {}
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    if set(config) - {"review", "roles", "workflow", "writer", "rounds", "revision", "knowledge", *REVISION_SECTIONS.values()}:
        raise ValueError("Unknown configuration section.")
    if set(config.get("roles", {})) - set(ROLE_NAMES):
        raise ValueError("Unknown reviewer in role configuration.")
    provider_keys = {"provider", "fixtures", "model", "timeout", "retries"}
    sections = [(config.get("review", {}), provider_keys | {"max_workers"}),
                (config.get("workflow", {}), provider_keys), (config.get("writer", {}), provider_keys),
                (config.get("rounds", {}), {"max_rounds"})]
    sections += [(entry, provider_keys) for entry in config.get("roles", {}).values()]
    sections += [(config.get(section, {}), provider_keys) for section in ("revision", *REVISION_SECTIONS.values())]
    for section, allowed in sections:
        if not isinstance(section, dict) or set(section) - allowed:
            raise ValueError("Unknown configuration field. Store API keys in environment variables only.")
        for key in ("max_workers", "max_rounds", "timeout", "retries"):
            if key in section and (isinstance(section[key], bool) or not isinstance(section[key], int)):
                raise ValueError(f"Configuration {key} must be an integer.")
    # File paths in a config are relative to that config, independent of cwd.
    for entry in [config.get("review", {}), config.get("workflow", {}), config.get("writer", {}),
                  *config.get("roles", {}).values(), *(config.get(section, {}) for section in ("revision", *REVISION_SECTIONS.values()))]:
        if "fixtures" in entry:
            entry["fixtures"] = str((path.parent / entry["fixtures"]).resolve())
    if "knowledge" in config:
        from .knowledge_sources import validate_settings
        config["knowledge"] = validate_settings(config["knowledge"], path.parent)
    return config


def make_provider(settings):
    kind = settings.get("provider", "fixture")
    if kind == "fixture":
        if not settings.get("fixtures") or settings.get("model"):
            raise ValueError("Fixture provider needs a fixture directory and no model.")
        return FixtureProvider(Path(settings["fixtures"]))
    if kind == "openai":
        if settings.get("fixtures"):
            raise ValueError("OpenAI provider cannot use fixtures.")
        model = settings.get("model") or os.environ.get("OPENAI_MODEL", "")
        return OpenAIProvider(model, timeout=settings.get("timeout", 90), retries=settings.get("retries", 1))
    raise ValueError("Provider must be fixture or openai.")


def review_provider(args, config):
    settings = dict(config.get("review", {}))
    for key in ("provider", "fixtures", "model"):
        value = getattr(args, key, None)
        if value is not None:
            settings[key] = str(value) if key == "fixtures" else value
    providers = {role: make_provider({**settings, **config.get("roles", {}).get(role, {})}) for role in ROLE_NAMES}
    return RoutedProvider(providers)


def task_provider(config, section="workflow"):
    settings = config.get(section, {})
    if settings.get("provider", "rule") == "rule":
        from .workflow import RuleWorkflowProvider
        return RuleWorkflowProvider()
    return make_provider(settings)


def revision_stage_settings(config, default_model=None):
    base = config.get("revision") or config.get("writer") or config.get("review", {})
    allowed = {"provider", "fixtures", "model", "timeout", "retries"}
    base = {key: value for key, value in base.items() if key in allowed}
    result = {stage: {"provider": "openai", **base, **config.get(section, {})} for stage, section in REVISION_SECTIONS.items()}
    for value in result.values():
        if value["provider"] == "openai" and not value.get("model"):
            value["model"] = default_model or os.environ.get("OPENAI_MODEL", "")
    return result


def revision_provider(config):
    settings = revision_stage_settings(config)
    if any(value.get("provider") == "rule" for value in settings.values()):
        raise ValueError("Full revision requires OpenAI or dedicated revision fixtures; the drafting rule composer is not a reviser.")
    return TaskRoutedProvider({stage: make_provider(value) for stage, value in settings.items()})
