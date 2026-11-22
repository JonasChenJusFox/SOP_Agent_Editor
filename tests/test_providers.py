import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admissions_editor.config import load_config, review_provider
from admissions_editor.providers import OpenAIProvider, ReviewerError, _TransientError
from admissions_editor.review import run_review
from admissions_editor.contracts import SCHEMAS
from admissions_editor.contracts import ContractError, validate


class ProviderTest(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_openai_requests_have_identical_inputs_distinct_roles_and_no_history(self):
        provider = OpenAIProvider("test-model", retries=0)
        captured = []

        def request(payload):
            captured.append(copy.deepcopy(payload))
            role = next(r for r in ("structure", "admissions", "language", "skeptical") if f"Role: {r}." in payload["instructions"])
            return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": json.dumps({"role": role, "findings": []})}]}]}

        with patch.object(provider, "_request", side_effect=request):
            run = run_review(provider, b"A synthetic draft.", "Describe a project.", "", {})
        self.assertEqual(run["status"], "complete")
        self.assertEqual(len(captured), 4)
        self.assertEqual(len({p["input"] for p in captured}), 1)
        self.assertEqual(len({p["instructions"] for p in captured}), 4)
        for payload in captured:
            self.assertFalse(payload["store"])
            self.assertNotIn("previous_response_id", payload)
            self.assertNotIn("conversation", payload)
            self.assertTrue(payload["text"]["format"]["strict"])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_transient_retries_are_bounded_and_fatal_errors_are_not_retried(self):
        provider = OpenAIProvider("test-model", retries=1)
        with patch.object(provider, "_request_once", side_effect=[_TransientError(), {"status": "completed"}]) as request, patch("admissions_editor.providers.time.sleep"):
            result = provider._request({"model": "test-model"})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["_attempts"], 2)
        self.assertEqual(request.call_args_list[0].args[1], request.call_args_list[1].args[1])
        with patch.object(provider, "_request_once", side_effect=ReviewerError("HTTP 401")) as request:
            with self.assertRaises(ReviewerError):
                provider._request({"model": "test-model"})
        self.assertEqual(request.call_count, 1)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder", "OPENAI_MODEL": "base-model"})
    def test_toml_routes_roles_and_uses_environment_default_without_persisting_key(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            path.write_text('[review]\nprovider="openai"\n[roles.skeptical]\nmodel="other-model"\n')
            config = load_config(path)
            provider = review_provider(SimpleNamespace(provider=None, fixtures=None, model=None), config)
            self.assertEqual(provider.label_for("structure"), "openai:base-model")
            self.assertEqual(provider.label_for("skeptical"), "openai:other-model")
            self.assertNotIn("synthetic-test-placeholder", json.dumps(config))

    def test_unknown_config_keys_fail_instead_of_silently_ignoring_secrets_or_typos(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text('[review]\napi_key="do-not-store"\n')
            with self.assertRaises(ValueError):
                load_config(path)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_workflow_wire_schema_restricts_evidence_ids_to_source_files(self):
        provider = OpenAIProvider("test-model", retries=0)
        for task, key in (("observe", "observations"), ("decide", "decisions"), ("targeted", "checks")):
            with self.subTest(task=task):
                original = copy.deepcopy(SCHEMAS[task])
                result = {key: []}
                task_input = {"snapshot": {"evidence": {"notes.txt": "Synthetic source."}}}
                if task == "decide":
                    task_input["groups"] = [{"id": "F-001", "paragraph_id": "P001", "quote": "An exact draft quote."}]
                    result = {"decisions": {"F-001": {"decision": "preserve", "rationale": "This clear sentence should remain.", "evidence_ids": ["notes.txt"]}}}
                response = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(result)}]}]}
                with patch.object(provider, "_request", return_value=response) as request:
                    provider.task(task, task_input, SCHEMAS[task])
                payload = request.call_args.args[0]
                wire = payload["text"]["format"]["schema"]
                entry = wire["properties"][key]["properties"]["F-001"] if task == "decide" else wire["properties"][key]["items"]
                refs = entry["properties"]["evidence_ids"]
                self.assertEqual(refs["items"]["enum"], ["notes.txt"])
                for field in ("paragraph_id", "quote", "assessment", "group_id", "issue_id", "rationale"):
                    if field in entry["properties"]:
                        self.assertNotIn("enum", entry["properties"][field], f"Evidence enum leaked into {task}.{field}")
                self.assertIn("Ledger claim IDs", payload["instructions"])
                self.assertIn("evidence_ids=[]", payload["instructions"])
                self.assertEqual(SCHEMAS[task], original)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_empty_evidence_requires_an_empty_reference_array(self):
        provider = OpenAIProvider("test-model", retries=0)
        response = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": '{"observations": []}'}]}]}
        with patch.object(provider, "_request", return_value=response) as request:
            provider.task("observe", {"snapshot": {"evidence": {}}}, SCHEMAS["observe"])
        refs = request.call_args.args[0]["text"]["format"]["schema"]["properties"]["observations"]["items"]["properties"]["evidence_ids"]
        self.assertEqual(refs["maxItems"], 0)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_eight_decision_groups_are_required_unique_keys_with_trusted_anchors(self):
        provider = OpenAIProvider("test-model", retries=0)
        groups = [{"id": f"F-{i:03d}", "paragraph_id": f"P{i:03d}", "quote": f"Exact synthetic passage {i}."} for i in range(1, 9)]
        output = {"decisions": {g["id"]: {"decision": "verify", "rationale": "A neutral source-based reason.",
                                         "evidence_ids": ["notes.txt"]} for g in reversed(groups)}}
        response = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": json.dumps(output)}]}]}
        with patch.object(provider, "_request", return_value=response) as request:
            result = provider.task("decide", {"snapshot": {"evidence": {"notes.txt": "Synthetic evidence."}}, "groups": groups}, SCHEMAS["decide"])
        wire = request.call_args.args[0]["text"]["format"]["schema"]
        self.assertEqual(set(wire["properties"]["decisions"]["required"]), {g["id"] for g in groups})
        self.assertFalse(wire["properties"]["decisions"]["additionalProperties"])
        validate(output, wire, "decide")
        validate(result, SCHEMAS["decide"], "stored decisions")
        for group, decision in zip(groups, result["decisions"]):
            self.assertEqual((decision["group_id"], decision["paragraph_id"], decision["quote"]),
                             (group["id"], group["paragraph_id"], group["quote"]))
        omitted = copy.deepcopy(output)
        omitted["decisions"].pop("F-008")
        with self.assertRaises(ContractError):
            validate(omitted, wire, "missing group")
        invented = copy.deepcopy(output)
        invented["decisions"]["unexpected"] = invented["decisions"]["F-001"]
        with self.assertRaises(ContractError):
            validate(invented, wire, "invented group")
        wrong_anchor = copy.deepcopy(output)
        wrong_anchor["decisions"]["F-001"]["quote"] = "A fabricated quote."
        with self.assertRaises(ContractError):
            validate(wrong_anchor, wire, "untrusted anchor")

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_no_groups_do_not_trigger_a_decision_api_request(self):
        provider = OpenAIProvider("test-model", retries=0)
        with patch.object(provider, "_request") as request:
            self.assertEqual(provider.task("decide", {"groups": []}, SCHEMAS["decide"]), {"decisions": []})
        request.assert_not_called()
        self.assertEqual(provider.call_records["decide"]["attempts"], 0)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-placeholder"})
    def test_source_enum_allows_valid_draft_anchors_and_free_text_assessments(self):
        provider = OpenAIProvider("test-model", retries=0)
        result = {"observations": [{"paragraph_id": "P001", "quote": "I built a parser.",
                  "assessment": "The source supports the parser contribution.", "evidence_ids": ["notes.txt"]}]}
        response = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": json.dumps(result)}]}]}
        with patch.object(provider, "_request", return_value=response) as request:
            provider.task("observe", {"snapshot": {"evidence": {"notes.txt": "Synthetic notes."}}}, SCHEMAS["observe"])
        wire = request.call_args.args[0]["text"]["format"]["schema"]
        validate(result, wire, "normal anchored observation")


if __name__ == "__main__":
    unittest.main()
