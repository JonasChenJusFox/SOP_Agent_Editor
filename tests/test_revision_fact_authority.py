"""Each writing pass must declare applicant facts before using new details."""

import unittest

from admissions_editor.revision import WRITING_STAGES, _mechanical
from admissions_editor.technical import extract_technical_context


class RevisionFactAuthorityTest(unittest.TestCase):
    def validation(self, extra, reported=(), *, stage_reports=None, evidence=None, requirements="Describe your contribution."):
        original = "I built the interface."
        fact = {"id": "FACT-DETAIL", "text": extra, "confirmed": True}
        snapshot = {"draft": original, "paragraphs": [{"id": "P1", "text": original}],
                    "requirements": requirements, "evidence": evidence or {}}
        plan = {"blockers": [], "unresolved_questions": [], "confirmed_facts": [fact],
                "findings": [], "protected_quotes": [], "word_limit": None,
                "technical_context": extract_technical_context(snapshot, confirmed_facts=[fact])}
        stages = [{"stage": name, "output": {"essay": original + " " + extra,
                   "addressed_finding_ids": [], "used_fact_ids": list((stage_reports or {}).get(name, reported)),
                   "unresolved_questions": [], "technical_flags": []}}
                  for name in WRITING_STAGES]
        return _mechanical({"snapshot": snapshot}, plan, stages)

    def test_nonmetric_count_needs_its_reported_fact_id(self):
        extra = "I processed 731 saved messages."
        omitted = self.validation(extra)
        reported = self.validation(extra, ["FACT-DETAIL"])
        self.assertTrue(any("新增未确认数字：731" in b for b in omitted["blockers"]))
        self.assertFalse(any("新增未确认数字" in b for b in reported["blockers"]), reported)

    def test_institution_name_needs_its_reported_fact_id(self):
        extra = "I studied at Riverbend Institute."
        omitted = self.validation(extra)
        reported = self.validation(extra, ["FACT-DETAIL"])
        self.assertEqual(omitted["factual_changes"]["humanize"]["added_named_phrases"], ["Riverbend Institute"])
        self.assertTrue(any("新增未确认人物/机构名称" in b for b in omitted["blockers"]))
        self.assertFalse(any("新增未确认人物/机构名称" in b for b in reported["blockers"]), reported)

    def test_ownership_expansion_needs_its_reported_fact_id(self):
        extra = "I independently built the interface."
        omitted = self.validation(extra)
        reported = self.validation(extra, ["FACT-DETAIL"])
        self.assertTrue(any("个人贡献范围可能被扩大" in b for b in omitted["blockers"]))
        self.assertFalse(any("个人贡献范围可能被扩大" in b for b in reported["blockers"]), reported)

    def test_an_earlier_pass_cannot_declare_sources_for_a_later_pass(self):
        result = self.validation("I processed 731 saved messages.",
                                 stage_reports={"sop_revise": ["FACT-DETAIL"], "humanize": ["FACT-DETAIL"]})
        numeric = [b for b in result["blockers"] if "新增未确认数字" in b]
        self.assertEqual(numeric, ["polish: 新增未确认数字：731。"])

    def test_unknown_id_and_evidence_do_not_silently_confirm_new_details(self):
        extra = "I processed 731 saved messages at Riverbend Institute."
        result = self.validation(extra, ["FACT-UNKNOWN"], evidence={"E1": extra})
        for name in WRITING_STAGES:
            self.assertIn(f"{name}: 引用了未确认或未知的作者事实。", result["blockers"])
            self.assertIn(f"{name}: 新增未确认数字：731。", result["blockers"])
            self.assertIn(f"{name}: 新增未确认人物/机构名称，请核对来源。", result["blockers"])

    def test_target_institution_wording_still_does_not_require_applicant_fact(self):
        result = self.validation("I hope to study at Riverbend Institute.",
                                 requirements="Why do you want to study at Riverbend Institute?")
        self.assertFalse(any("新增未确认人物/机构名称" in b for b in result["blockers"]), result)


if __name__ == "__main__":
    unittest.main()
