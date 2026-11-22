"""Deterministic, dependency-free Word exports for local and native Workers.

Exports are assembled from validated stored records, never from request-supplied
essay text. No Office process, remote conversion service or binary DB is needed.
"""

import difflib
import io
import re
import zipfile
from xml.etree import ElementTree as ET

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ET.register_namespace("w", W)
ET.register_namespace("r", R)


def _w(parent, name, value=None, **attributes):
    attrs = {"{" + W + "}" + key: str(item) for key, item in attributes.items()}
    if value is not None:
        attrs["{" + W + "}val"] = str(value)
    return ET.SubElement(parent, "{" + W + "}" + name, attrs)


def _text(value):
    return "".join(c for c in str(value) if c in "\n\t\r" or 32 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)


def _paragraph(body, text, style="Normal", numbered=False):
    paragraph = _w(body, "p")
    properties = _w(paragraph, "pPr")
    _w(properties, "pStyle", style)
    if numbered:
        number = _w(properties, "numPr")
        _w(number, "ilvl", 0)
        _w(number, "numId", 1)
    parts = _text(text).split("\n")
    for index, part in enumerate(parts):
        run = _w(paragraph, "r")
        if index:
            _w(run, "br")
        node = _w(run, "t")
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        node.text = part
    return paragraph


def _styles():
    # standard_business_brief: Letter/1in, Calibri11, 6pt after, 1.10 spacing.
    root = ET.Element("{" + W + "}styles")
    defaults = _w(root, "docDefaults")
    run_defaults = _w(_w(defaults, "rPrDefault"), "rPr")
    _w(run_defaults, "rFonts", ascii="Calibri", hAnsi="Calibri", eastAsia="PingFang SC")
    _w(run_defaults, "sz", 22)
    _w(run_defaults, "lang", "en-US", eastAsia="zh-CN")
    for name, size, before, after, color, bold in (
        ("Normal", 22, 0, 120, "222222", False),
        ("Title", 40, 0, 160, "222222", True),
        ("Subtitle", 22, 0, 120, "555555", False),
        ("Heading1", 32, 320, 160, "2E74B5", True),
        ("Heading2", 26, 240, 120, "2E74B5", True),
        ("Heading3", 24, 160, 80, "1F4D78", True),
        ("ListParagraph", 22, 0, 160, "222222", False),
    ):
        style = _w(root, "style", type="paragraph", styleId=name)
        if name == "Normal":
            style.set("{" + W + "}default", "1")
        _w(style, "name", name)
        if name != "Normal":
            _w(style, "basedOn", "Normal")
        props = _w(style, "pPr")
        _w(props, "spacing", before=before, after=after, line=280 if name == "ListParagraph" else 264, lineRule="auto")
        _w(props, "widowControl")
        if name.startswith("Heading") or name == "Title":
            _w(props, "keepNext")
            _w(props, "keepLines")
        if name.startswith("Heading"):
            _w(props, "outlineLvl", int(name[-1]) - 1)
        run = _w(style, "rPr")
        _w(run, "sz", size)
        _w(run, "color", color)
        if bold:
            _w(run, "b")
    return root


def _numbering():
    root = ET.Element("{" + W + "}numbering")
    abstract = _w(root, "abstractNum", abstractNumId=0)
    _w(abstract, "multiLevelType", "singleLevel")
    level = _w(abstract, "lvl", ilvl=0)
    _w(level, "start", 1)
    _w(level, "numFmt", "decimal")
    _w(level, "lvlText", "%1.")
    _w(level, "lvlJc", "left")
    props = _w(level, "pPr")
    _w(_w(props, "tabs"), "tab", val="num", pos=720)
    _w(props, "ind", left=720, hanging=360)
    number = _w(root, "num", numId=1)
    _w(number, "abstractNumId", 0)
    return root


def _xml(element):
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _docx(rows, metadata=""):
    document = ET.Element("{" + W + "}document")
    body = _w(document, "body")
    for style, text, numbered in rows:
        _paragraph(body, text, style, numbered)
    section = _w(body, "sectPr")
    _w(section, "pgSz", w=12240, h=15840)
    _w(section, "pgMar", top=1440, right=1440, bottom=1440, left=1440, header=708, footer=708, gutter=0)
    package = {
        "[Content_Types].xml": b'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>''',
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>''',
        "word/_rels/document.xml.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/></Relationships>''',
        "word/document.xml": _xml(document), "word/styles.xml": _xml(_styles()), "word/numbering.xml": _xml(_numbering()),
    }
    core = ET.Element("{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties")
    ET.SubElement(core, "{http://purl.org/dc/elements/1.1/}creator").text = "Admissions Editor"
    ET.SubElement(core, "{http://purl.org/dc/elements/1.1/}description").text = _text(metadata)
    package["docProps/core.xml"] = _xml(core)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in package.items():
            info = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return stream.getvalue()


def _paragraphs(text):
    return [part.strip() for part in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if part.strip()]


def editorial_document(report, run, run_sha256):
    from .editorial import validate_editorial_report
    validate_editorial_report(run, run_sha256, report)
    rows = [("Title", "综合审稿建议", False), ("Normal", report["summary"], False)]
    for issue in report["issues"]:
        rows += [("ListParagraph", issue["title"], True), ("Normal", "问题：" + issue["problem"], False),
                 ("Normal", "建议：" + issue["recommendation"], False)]
    return _docx(rows, "review=" + report["review_sha256"] + "; report=" + report["report_sha256"])


def revision_document(record, filename):
    """Return an essay or truthful change report; blocked text stays a candidate."""
    from .revision_web import revision_result
    view = revision_result(record, "", [])  # checks integrity/policy before export
    ready = view["status"] == "ready" and view["validation"].get("passed") is True
    if filename not in {"essay.docx", "change-report.docx"}:
        raise ValueError("Unknown Word document.")
    metadata = "candidate=" + record["candidate_sha256"] + "; plan=" + record["plan"]["plan_sha256"]
    if filename == "essay.docx":
        rows = [] if ready else [("Title", "候选文章 · 核验尚未通过", False),
            ("Normal", "以下为保留的候选文本，尚不能作为通过核验的优化稿。待处理项见修改报告。", False)]
        rows += [("Normal", paragraph, False) for paragraph in _paragraphs(record["candidate"])]
        return _docx(rows, metadata)
    rows = [("Title", "文章修改报告" if ready else "候选文章修改报告 · 待处理", False),
            ("Normal", "状态：全文核验通过，仍请作者核对后采用。" if ready else "状态：候选尚有阻塞项；本报告不代表修改已经全部完成。", False)]
    context = record["plan"].get("editorial_context") or {}
    if context:
        rows += [("Heading1", "整体评价与修改方向", False), ("Normal", context["feedback"], False)]
        summary = context.get("interpretation", {}).get("summary")
        if summary:
            rows.append(("Normal", "模型解析：" + summary, False))
        if context.get("normalization_notes"):
            rows.append(("Heading2", "评价处理说明", False))
            for note in context["normalization_notes"]:
                rows.append(("Normal", ("待核实：" if note["blocking"] else "说明：") + note["message"], False))
    names = {"sop_revise": "SOP Reviser", "polish": "Language Polisher", "humanize": "Humanizer"}
    skills = context.get("selected_skill_ids", ["sop_revise", "polish", "humanize"])
    rows.append(("Normal", "重点匹配的编辑 skills：" + "、".join(names.get(s, s) for s in skills), False))
    performed = [names.get(stage["stage"], "只读核验") for stage in record.get("stages", [])]
    rows += [("Normal", "已完成阶段：" + (" → ".join(performed) or "尚未完成写作阶段"), False),
             ("Heading1", "意见处理与修改理由", False)]
    for item in record.get("trace", []):
        label = {"addressed": "已处理", "reject": "保留原文", "defer": "暂缓", "unresolved": "待处理"}.get(item["status"], item["status"])
        rows.append(("ListParagraph", item["finding_id"] + " · " + label, True))
        rows.append(("Normal", "修改方向：" + item["reason"], False))
        rows.append(("Normal", "原文：" + item["original_quote"], False))
        if item.get("final_quote"):
            rows.append(("Normal", "修改后：" + item["final_quote"], False))
        if item.get("verification_rationale"):
            rows.append(("Normal", "核验说明：" + item["verification_rationale"], False))
    rows.append(("Heading1", "实际段落变动", False))
    before, after = _paragraphs(record["source_review"]["snapshot"]["draft"]), _paragraphs(record["candidate"])
    count = 0
    for operation, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if operation == "equal":
            continue
        count += 1
        rows.append(("ListParagraph", {"replace": "重写或重组", "delete": "删除", "insert": "新增"}[operation], True))
        if b > a:
            rows.append(("Normal", "原文：\n" + "\n\n".join(before[a:b]), False))
        if d > c:
            rows.append(("Normal", "修改后：\n" + "\n\n".join(after[c:d]), False))
    if not count:
        rows.append(("Normal", "候选与原稿尚无实际段落改动。", False))
    claims = record.get("validation", {}).get("claim_changes", [])
    if claims:
        rows.append(("Heading1", "事实与来源追溯", False))
        for claim in claims:
            rows.append(("Normal", f"{claim['source_type']} · {claim['source_id']}\n来源片段：{claim['source_quote']}", False))
    rows.append(("Heading1", "核验与待处理事项", False))
    for blocker in view.get("blockers", []):
        rows.append(("ListParagraph", blocker, True))
    for issue in record.get("technical_accuracy", {}).get("issues", []):
        rows.append(("Normal", issue["reason"], False))
        if issue.get("source_quote"):
            rows.append(("Normal", "来源：" + issue.get("source_id", "") + "\n" + issue["source_quote"], False))
    if not view.get("blockers"):
        rows.append(("Normal", "结构、事实、申请要求、作者反馈及技术准确性已完成当前流程核验；这不是对事实真实性或录取结果的保证。", False))
    return _docx(rows, metadata)
