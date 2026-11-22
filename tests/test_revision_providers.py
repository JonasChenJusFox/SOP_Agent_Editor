"""Revision request contracts and model selection; all transport is mocked."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from admissions_editor.config import (REVISION_SECTIONS, load_config, revision_provider,
                                       revision_stage_settings)
from admissions_editor.contracts import SCHEMAS, validate
from admissions_editor.editorial_skills import STAGES, load_editorial_skill
from admissions_editor.providers import (REVIEW_RULES, FixtureProvider, OpenAIProvider,
                                          ReviewerError, TaskRoutedProvider, review_schema)
from admissions_editor.revision_schema import REVISION_SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/revision/fixtures"
KEY = "synthetic-revision-provider-key"


def fixture(stage):
    return json.loads((FIXTURES / f"{stage}.json").read_text())


def response(value):
    return {"status": "completed", "usage": {"input_tokens": 24, "output_tokens": 48},
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": json.dumps(value)}]}]}


class RevisionOpenAIRequestTest(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": KEY})
    def test_four_stage_http_requests_use_packaged_instructions_and_exact_schemas(self):
        reviewer_before = review_schema(strict=True)
        revisions_before = copy.deepcopy(REVISION_SCHEMAS)
        for stage in STAGES:
            with self.subTest(stage=stage):
                provider = OpenAIProvider(f"synthetic-{stage}", timeout=23, retries=0)
                data = {"original": {"draft": "A synthetic draft.", "evidence": {"notes.txt": "Synthetic notes."}},
                        "current_essay": "A synthetic candidate.", "revision_plan": {"findings": []},
                        "requirements": "Describe the project in plain English."}
                expected = fixture(stage)
                with patch("admissions_editor.providers.http.client.HTTPSConnection") as transport:
                    connection = transport.return_value
                    connection.getresponse.return_value.status = 200
                    connection.getresponse.return_value.read.return_value = json.dumps(response(expected)).encode()
                    result = provider.task(stage, data, REVISION_SCHEMAS[stage])
                transport.assert_called_once_with("api.openai.com", timeout=23)
                connection.close.assert_called_once()
                args, kwargs = connection.request.call_args
                self.assertEqual(args, ("POST", "/v1/responses"))
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + KEY)
                self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
                self.assertEqual(len(kwargs["headers"]["Idempotency-Key"]), 64)
                wire = json.loads(kwargs["body"])
                self.assertEqual(wire["model"], f"synthetic-{stage}")
                self.assertFalse(wire["store"])
                self.assertEqual(json.loads(wire["input"]), data)
                self.assertTrue(wire["instructions"].startswith(load_editorial_skill(stage)["instructions"]))
                self.assertNotIn(REVIEW_RULES, wire["instructions"])
                self.assertNotIn(KEY, json.dumps(wire))
                self.assertNotIn("conversation", wire)
                self.assertNotIn("previous_response_id", wire)
                structured = wire["text"]["format"]
                self.assertEqual(structured["type"], "json_schema")
                self.assertEqual(structured["name"], stage)
                self.assertTrue(structured["strict"])
                self.assertEqual(structured["schema"], REVISION_SCHEMAS[stage])
                self.assertNotIn("role", structured["schema"]["properties"])
                self.assertNotIn("findings", structured["schema"]["properties"])
                self.assertEqual(wire["max_output_tokens"], 10000 if stage == "verify_revision" else 16000)
                self.assertEqual(result, expected)
                validate(result, structured["schema"], stage)
                self.assertEqual(provider.call_records[stage],
                    {"usage": {"input_tokens": 24, "output_tokens": 48}, "attempts": 1})
        self.assertEqual(review_schema(strict=True), reviewer_before)
        self.assertEqual(REVISION_SCHEMAS, revisions_before)

    @patch.dict(os.environ, {"OPENAI_API_KEY": KEY})
    def test_workflow_evidence_enum_does_not_leak_into_revision_contracts(self):
        provider = OpenAIProvider("synthetic-model", retries=0)
        before = copy.deepcopy(REVISION_SCHEMAS)
        with patch.object(provider, "_request", return_value=response({"observations": []})) as request:
            provider.task("observe", {"snapshot": {"evidence": {"one-source.txt": "Synthetic source."}}}, SCHEMAS["observe"])
        observe = request.call_args.args[0]["text"]["format"]["schema"]
        self.assertEqual(observe["properties"]["observations"]["items"]["properties"]["evidence_ids"]["items"]["enum"],
                         ["one-source.txt"])
        for stage in STAGES:
            with self.subTest(stage=stage):
                with patch.object(provider, "_request", return_value=response(fixture(stage))) as request:
                    provider.task(stage, {"original": {"evidence": {}}}, REVISION_SCHEMAS[stage])
                self.assertEqual(request.call_args.args[0]["text"]["format"]["schema"], before[stage])
        self.assertEqual(REVISION_SCHEMAS, before)

    @patch.dict(os.environ, {"OPENAI_API_KEY": KEY})
    def test_failed_transport_keeps_stage_metrics_without_secret_or_payload_echo(self):
        provider = OpenAIProvider("synthetic-model", retries=0)
        with patch("admissions_editor.providers.http.client.HTTPSConnection") as transport:
            transport.return_value.getresponse.return_value.status = 401
            with self.assertRaises(ReviewerError) as raised:
                provider.task("humanize", {"current_essay": "PRIVATE-SYNTHETIC-CONTENT"}, REVISION_SCHEMAS["humanize"])
        self.assertEqual(provider.call_records["humanize"], {"usage": {}, "attempts": 1})
        self.assertNotIn(KEY, str(raised.exception))
        self.assertNotIn("PRIVATE-SYNTHETIC-CONTENT", str(raised.exception))


class RevisionRoutingTest(unittest.TestCase):
    def test_stage_routes_have_independent_providers_and_copied_metrics(self):
        providers = {}
        for index, stage in enumerate(STAGES, 1):
            provider = Mock()
            provider.label = "synthetic:" + stage
            provider.task.return_value = fixture(stage)
            provider.call_records = {stage: {"usage": {"input_tokens": index}, "attempts": 1}}
            providers[stage] = provider
        routed = TaskRoutedProvider(providers)
        for stage in STAGES:
            payload = {"stage": stage, "current_essay": "Synthetic candidate."}
            result = routed.task(stage, payload, REVISION_SCHEMAS[stage])
            providers[stage].task.assert_called_once_with(stage, payload, REVISION_SCHEMAS[stage])
            self.assertEqual(result, fixture(stage))
            self.assertEqual(routed.label_for(stage), "synthetic:" + stage)
            saved = copy.deepcopy(routed.call_records[stage])
            providers[stage].call_records[stage]["usage"]["input_tokens"] = 999
            self.assertEqual(routed.call_records[stage], saved)
        self.assertEqual(set(routed.call_records), set(STAGES))
        with self.assertRaisesRegex(ValueError, "Unknown revision stage"):
            routed.task("admissions", {}, {})

    def test_failed_route_still_copies_that_stage_metrics(self):
        provider = Mock()
        provider.label = "synthetic:failing-verifier"
        provider.call_records = {"verify_revision": {"usage": {}, "attempts": 2}}
        provider.task.side_effect = ReviewerError("Synthetic failure.")
        routed = TaskRoutedProvider({"verify_revision": provider})
        with self.assertRaises(ReviewerError):
            routed.task("verify_revision", {}, REVISION_SCHEMAS["verify_revision"])
        self.assertEqual(routed.call_records["verify_revision"], {"usage": {}, "attempts": 2})
        provider.call_records["verify_revision"]["attempts"] = 3
        self.assertEqual(routed.call_records["verify_revision"]["attempts"], 2)


class RevisionConfigTest(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": "environment-model"})
    def test_environment_defaults_cloud_default_and_per_stage_models(self):
        defaults = revision_stage_settings({})
        self.assertEqual(set(defaults), set(STAGES))
        self.assertTrue(all(value == {"provider": "openai", "model": "environment-model"} for value in defaults.values()))
        self.assertTrue(all(value["model"] == "binding-model" for value in revision_stage_settings({}, "binding-model").values()))
        config = {"review": {"provider": "openai", "model": "review-model", "max_workers": 2},
                  "writer": {"provider": "openai", "model": "writer-model"},
                  "revision": {"provider": "openai", "model": "revision-model", "timeout": 45, "retries": 0},
                  "reviser": {"model": "substantive-model"},
                  "humanizer": {"model": "voice-model", "timeout": 60},
                  "revision_verifier": {"model": "verification-model", "retries": 1}}
        original = copy.deepcopy(config)
        settings = revision_stage_settings(config)
        expected_models = {"sop_revise": "substantive-model", "polish": "revision-model",
                           "humanize": "voice-model", "verify_revision": "verification-model"}
        routed = revision_provider(config)
        for stage, model in expected_models.items():
            self.assertEqual(settings[stage]["model"], model)
            self.assertEqual(routed.label_for(stage), "openai:" + model)
        self.assertEqual(routed.providers["humanize"].timeout, 60)
        self.assertEqual(routed.providers["verify_revision"].retries, 1)
        self.assertNotIn("max_workers", json.dumps(settings))
        self.assertNotIn(KEY, json.dumps(settings))
        self.assertEqual(config, original)
        self.assertEqual(len({id(provider) for provider in routed.providers.values()}), 4)

    @patch.dict(os.environ, {"OPENAI_MODEL": "environment-model"})
    def test_revision_then_writer_then_review_default_precedence(self):
        cases = [
            ({"review": {"provider": "openai", "model": "review-model", "max_workers": 3}}, "review-model"),
            ({"review": {"model": "review-model"}, "writer": {"provider": "openai", "model": "writer-model"}}, "writer-model"),
            ({"writer": {"model": "writer-model"}, "revision": {"provider": "openai", "model": "revision-model"}}, "revision-model"),
        ]
        for config, model in cases:
            with self.subTest(model=model):
                settings = revision_stage_settings(config)
                self.assertTrue(all(value["model"] == model for value in settings.values()))
                self.assertTrue(all("max_workers" not in value for value in settings.values()))

    def test_fixture_paths_are_relative_to_toml_for_base_and_stage_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings").mkdir()
            for name in ("shared", "humanizer"):
                target = root / name
                target.mkdir()
                for source in FIXTURES.glob("*.json"):
                    shutil.copyfile(source, target / source.name)
            path = root / "settings/models.toml"
            path.write_text('[revision]\nprovider="fixture"\nfixtures="../shared"\n\n'
                            '[humanizer]\nfixtures="../humanizer"\n')
            config = load_config(path)
            self.assertEqual(config["revision"]["fixtures"], str((root / "shared").resolve()))
            self.assertEqual(config["humanizer"]["fixtures"], str((root / "humanizer").resolve()))
            provider = revision_provider(config)
            for stage in STAGES:
                self.assertIsInstance(provider.providers[stage], FixtureProvider)
                expected_path = root / ("humanizer" if stage == "humanize" else "shared")
                self.assertEqual(provider.providers[stage].fixture_dir, expected_path.resolve())
                self.assertEqual(provider.task(stage, {}, REVISION_SCHEMAS[stage]), fixture(stage))

    def test_rule_composer_is_rejected_for_entire_revision_or_individual_stage(self):
        for config in ({"revision": {"provider": "rule"}}, {"writer": {"provider": "rule"}},
                       {"revision": {"provider": "openai"}, "humanizer": {"provider": "rule"}}):
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "not a reviser"):
                    revision_provider(config)

    def test_revision_sections_reject_secret_fields_and_invalid_numeric_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            for section in ("revision", *REVISION_SECTIONS.values()):
                for field in ('api_key="synthetic-do-not-store"', 'timeout=true', 'retries="1"'):
                    with self.subTest(section=section, field=field):
                        path.write_text(f"[{section}]\n{field}\n")
                        with self.assertRaises(ValueError):
                            load_config(path)

    @patch.dict(os.environ, {"OPENAI_API_KEY": KEY, "OPENAI_MODEL": ""})
    def test_missing_model_and_key_fail_before_any_http_request(self):
        with patch("admissions_editor.providers.http.client.HTTPSConnection") as transport:
            with self.assertRaisesRegex(ValueError, "Choose an OpenAI model"):
                revision_provider({})
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                    revision_provider({"revision": {"model": "synthetic-model"}})
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
