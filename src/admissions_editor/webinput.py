"""Portable Web input validation shared by local and Cloudflare runtimes."""

from datetime import datetime
from pathlib import Path

from .facts import build_ledger
from .review import paragraph_spans

MAX_BODY = 512 * 1024
MAX_TEXT = 128 * 1024


class WebError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)



def normalize_input(value):
    if not isinstance(value, dict):
        raise WebError(400, "请求需要是 JSON 对象。")
    result = {}
    for name in ("draft", "requirements", "preferences"):
        text = value.get(name, "")
        if not isinstance(text, str) or "\x00" in text or len(text.encode()) > MAX_TEXT:
            raise WebError(400, "文本格式不正确或单项超过 128 KB。")
        result[name] = text
    if not result["draft"].strip() or not result["requirements"].strip():
        raise WebError(400, "请提供原稿和申请题目。")
    evidence = value.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 12:
        raise WebError(400, "证据最多支持 12 个文件。")
    result["evidence"] = {}
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
            raise WebError(400, "证据文件格式不正确。")
        name, text = item["id"], item["text"]
        if not name or len(name) > 120 or Path(name).name != name or any(c in name for c in "\\/\x00\r\n") or Path(name).suffix.lower() not in {".txt", ".md", ".pdf", ".docx"}:
            raise WebError(400, "证据 ID 需要是 .txt、.md、.pdf 或 .docx 文件名；内容须为提取后的文字。")
        if name in result["evidence"] or "\x00" in text or len(text.encode()) > MAX_TEXT:
            raise WebError(400, "证据文件名重复、格式不正确或超过 128 KB。")
        result["evidence"][name] = text
    result["claims"] = value.get("claims", {"claims": []})
    result["sources"] = value.get("sources", {})
    result["as_of"] = value.get("as_of") or datetime.now().astimezone().date().isoformat()
    if not isinstance(result["as_of"], str):
        raise WebError(400, "核验日期需要是 YYYY-MM-DD。")
    snapshot = {"draft": result["draft"], "paragraphs": [{"id": k, "text": v[1]} for k, v in paragraph_spans(result["draft"]).items()],
                "evidence": result["evidence"]}
    try:
        build_ledger(snapshot, result["claims"], result["sources"], result["as_of"])
    except (ValueError, TypeError, AttributeError, KeyError):
        raise WebError(400, "主张或来源映射校验失败，请核对 JSON、原文引句与证据片段。") from None
    return result
