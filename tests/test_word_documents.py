"""Word downloads preserve validated essay paragraphs and truthful change traces."""

import copy
import io
import unittest
import zipfile
from xml.etree import ElementTree as ET

from admissions_editor.editorial import build_editorial_report
from admissions_editor.review import sha256
from admissions_editor.revision import json_bytes, prepare_revision_plan, revise_document
from admissions_editor.webinput import WebError
from admissions_editor.word_documents import W, editorial_document, revision_document
from test_revision import Editor, make_source


def contents(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


def paragraphs(data):
    root = ET.fromstring(contents(data)["word/document.xml"])
    return ["".join(node.text or "" if node.tag == "{" + W + "}t" else "\n" if node.tag == "{" + W + "}br" else ""
                    for node in p.iter()) for p in root.findall(".//{" + W + "}body/{" + W + "}p")]


class WordDocumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review_run, options = make_source()
        cls.run_sha = sha256(json_bytes(cls.review_run))
        plan = prepare_revision_plan(cls.review_run, cls.run_sha, **options)
        cls.record = revise_document(cls.review_run, plan, Editor())

    def test_essay_is_real_word_package_with_exact_paragraphs_and_no_report_prose(self):
        data = revision_document(self.record, "essay.docx")
        self.assertEqual(data[:2], b"PK")
        self.assertEqual(paragraphs(data), self.record["candidate"].strip().split("\n\n"))
        parts = contents(data)
        for name in ("[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels",
                     "word/document.xml", "word/styles.xml", "word/numbering.xml", "docProps/core.xml"):
            ET.fromstring(parts[name])
        document = ET.fromstring(parts["word/document.xml"])
        page = document.find(".//{" + W + "}pgSz")
        self.assertEqual(page.get("{" + W + "}w"), "12240")
        self.assertEqual(document.find(".//{" + W + "}pgMar").get("{" + W + "}left"), "1440")
        self.assertEqual(revision_document(self.record, "essay.docx"), data)

    def test_change_report_describes_actual_changes_sources_and_verification(self):
        data = revision_document(self.record, "change-report.docx")
        text = "\n".join(paragraphs(data))
        for expected in ("文章修改报告", "实际段落变动", "事实与来源追溯", "核验与待处理事项",
                         "20 saved messages", "FACT-RESULT", "SOP Reviser"):
            self.assertIn(expected, text)
        self.assertIn(self.review_run["snapshot"]["draft"].split("\n\n")[0], text)
        document = ET.fromstring(contents(data)["word/document.xml"])
        self.assertTrue(document.findall(".//{" + W + "}numPr"))
        numbering = ET.fromstring(contents(data)["word/numbering.xml"])
        self.assertEqual(numbering.find(".//{" + W + "}numFmt").get("{" + W + "}val"), "decimal")

    def test_blocked_candidate_download_is_clearly_labeled_and_never_claims_success(self):
        _, options = make_source()
        options["answers"] = {}
        plan = prepare_revision_plan(self.review_run, self.run_sha, **options)
        record = revise_document(self.review_run, plan, Editor())
        self.assertEqual(record["status"], "blocked")
        essay = "\n".join(paragraphs(revision_document(record, "essay.docx")))
        report = "\n".join(paragraphs(revision_document(record, "change-report.docx")))
        self.assertIn("候选文章 · 核验尚未通过", essay)
        self.assertIn("候选尚有阻塞项", report)
        self.assertNotIn("状态：全文核验通过", report)

    def test_tampered_record_and_unknown_document_cannot_export(self):
        record = copy.deepcopy(self.record)
        record["candidate"] += " Unverified additional claim."
        with self.assertRaises(WebError):
            revision_document(record, "essay.docx")
        with self.assertRaises(ValueError):
            revision_document(self.record, "arbitrary.docx")

    def test_consolidated_report_roundtrips_chinese_and_has_real_numbering(self):
        report = build_editorial_report(self.review_run, self.run_sha)
        data = editorial_document(report, self.review_run, self.run_sha)
        text = "\n".join(paragraphs(data))
        self.assertIn(report["summary"], text)
        for issue in report["issues"]:
            self.assertIn(issue["title"], text)
            self.assertIn(issue["recommendation"], text)
        report["summary"] += " Changed."
        with self.assertRaises(ValueError):
            editorial_document(report, self.review_run, self.run_sha)

    def test_change_report_explains_excluded_optional_answer_without_promoting_it(self):
        from admissions_editor.cloud_runtime import revision_demo_payload
        from admissions_editor.editorial import prepare_feedback_plan
        from test_editorial import Model
        from test_revision import EXAMPLES
        _, options = make_source()
        manifest = revision_demo_payload(EXAMPLES)
        feedback = manifest["feedback"] + "\nIf I built a model, it might sound more impressive."
        output = {"summary": "Preserve the supplied facts and revise the language.",
                  "decisions": options["decisions"],
                  "answer_excerpts": [{"finding_id": key, "quote": value} for key, value in options["answers"].items()] +
                      [{"finding_id": "language-01", "quote": "I built a model"}],
                  "selected_skill_ids": ["sop_revise", "polish", "humanize"], "unresolved_questions": []}
        report = build_editorial_report(self.review_run, self.run_sha)
        plan = prepare_feedback_plan(self.review_run, self.run_sha, report,
            {"feedback": feedback, "confirmed_facts": options["confirmed_facts"]}, Model(output))
        self.assertNotIn("language-01", plan["answers"])
        record = revise_document(self.review_run, plan, Editor())
        self.assertEqual(record["status"], "ready", record["validation"])
        text = "\n".join(paragraphs(revision_document(record, "change-report.docx")))
        self.assertIn("评价处理说明", text)
        self.assertIn("已排除", text)
        self.assertNotIn("I built a model", record["candidate"])


if __name__ == "__main__":
    unittest.main()
