"""Validate source and model data before it reaches Notion."""
from __future__ import annotations

from urllib.parse import urlsplit
import re


class CardError(ValueError):
    pass


LIMITS = {
    "source_url": 2048, "canonical_url": 2048, "title": 300,
    "platform": 20, "content_type": 20, "author": 200, "summary": 4000,
    "source_text": 50000, "evidence": 20, "evidence_note": 1000,
}
LISTS = {"points": (12, 500), "actions": (12, 500), "tags": (8, 30)}
EVIDENCE = {"full_text", "asr", "partial", "metadata", "none"}


def validate_card(card: dict) -> dict:
    from .network import validate_source_url, normalize_source_url
    if not isinstance(card, dict) or set(card) != set(LIMITS) | set(LISTS):
        raise CardError("笔记内容格式不符合要求，已停止保存。")
    for key, limit in LIMITS.items():
        value = card[key]
        if not isinstance(value, str) or len(value) > limit or re.search(r"[\x00\ud800-\udfff]", value):
            raise CardError("笔记内容过长或格式异常，已停止保存。")
    for key, (count, length) in LISTS.items():
        value = card[key]
        if not isinstance(value, list) or len(value) > count or any(
            not isinstance(item, str) or len(item) > length or re.search(r"[\x00\ud800-\udfff]", item) for item in value
        ):
            raise CardError("笔记列表格式异常，已停止保存。")
    if card["platform"] not in {"douyin", "xiaohongshu", "wechat"}:
        raise CardError("暂不支持这个来源。")
    if card["content_type"] not in {"video", "article", "note", "unknown"}:
        raise CardError("无法确认内容类型。")
    if card["evidence"] not in EVIDENCE:
        raise CardError("无法确认笔记依据。")
    if validate_source_url(card["source_url"]) != card["platform"]:
        raise CardError("来源平台与地址不一致。")
    if validate_source_url(card["canonical_url"]) != card["platform"]:
        raise CardError("规范地址指向了不同平台。")
    if normalize_source_url(card["canonical_url"]) != card["canonical_url"]:
        raise CardError("来源地址没有规范化。")
    if card["evidence"] in {"metadata", "none"}:
        if card["source_text"] or card["summary"] or card["points"] or card["actions"]:
            raise CardError("没有读到正文，不能生成正文摘要。")
    elif not card["source_text"].strip():
        raise CardError("正文依据为空，不能标记为已读正文。")
    return card


def notion_page_url(value: str) -> str:
    """Only allow actual Notion page URLs, not arbitrary returned links."""
    if not isinstance(value, str) or len(value) > 2048:
        raise CardError("Notion 返回了异常地址。")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise CardError("Notion 返回了异常地址。") from None
    if (parts.scheme != "https" or parts.hostname not in {"www.notion.so", "notion.so"}
            or parts.username or parts.password or port not in {None, 443}
            or parts.query or parts.fragment
            or not re.search(r"[0-9a-fA-F]{32}$|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", parts.path)):
        raise CardError("Notion 返回了异常地址。")
    return value
