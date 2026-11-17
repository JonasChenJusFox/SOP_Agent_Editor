import copy
import unittest
from threading import Lock

from admissions_editor.providers import ROLE_NAMES
from admissions_editor.review import run_review


class RecordingProvider:
    label = "recording-provider"

    def __init__(self):
        self.lock = Lock()
        self.inputs = {}

    def review(self, role, review_input):
        with self.lock:
            self.inputs[role] = copy.deepcopy(review_input)
        review_input["paragraphs"][0]["text"] = f"{role} private note"
        return {"role": role, "findings": []}


class ReviewIsolationTest(unittest.TestCase):
    def test_reviewers_receive_equal_inputs_without_sharing_evolving_request_objects(self):
        provider = RecordingProvider()
        draft = "I studied software systems."
        run = run_review(provider, draft.encode(), "Explain your interests.", "", {})
        self.assertEqual(run["status"], "complete")
        self.assertEqual(set(provider.inputs), set(ROLE_NAMES))
        for payload in provider.inputs.values():
            self.assertEqual(payload["draft"], draft)
            self.assertEqual(payload["paragraphs"], [{"id": "P001", "text": draft}])
            self.assertEqual(set(payload), {"draft", "paragraphs", "requirements", "preferences", "evidence"})
        self.assertEqual(run["groups"], [])
