"""JSON Feed 1.0/1.1 parser with tolerant scalar handling."""

from __future__ import annotations

import json
from typing import Any, Optional

from ..models import Enclosure, ItemIssue, ParsedFeed, ParsedItem
from .common import (
    ParseError,
    normalize_text,
    normalize_title,
    normalize_url,
    parse_declared_length,
    parse_iso_datetime,
    unique_strings,
)


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _raw_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise ParseError("JSONFEED_ITEM_BAD_ID", "id 必须是字符串或数字")


def _attachments(item: dict[str, Any]) -> tuple[Enclosure, ...]:
    raw = item.get("attachments")
    if not isinstance(raw, list):
        return ()
    found: list[Enclosure] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            continue
        url = normalize_url(attachment.get("url"))
        if not url:
            continue
        mime = _text(attachment.get("mime_type"))
        length = parse_declared_length(attachment.get("size_in_bytes"))
        found.append(Enclosure(url, mime, length, len(found)))
    return tuple(found)


def _authors(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    authors = item.get("authors")
    if isinstance(authors, list):
        for author in authors:
            if isinstance(author, dict):
                values.append(_text(author.get("name")) or "")
            elif isinstance(author, str):
                values.append(author)
    legacy = item.get("author")
    if isinstance(legacy, dict):
        values.append(_text(legacy.get("name")) or "")
    elif isinstance(legacy, str):
        values.append(legacy)
    return unique_strings(values)


def _parse_item(raw: Any, index: int) -> ParsedItem:
    if not isinstance(raw, dict):
        raise ParseError("JSONFEED_ITEM_NOT_OBJECT", f"条目 {index} 不是对象")
    published_raw = raw.get("date_published") or raw.get("date_modified")
    if published_raw is not None and not isinstance(published_raw, str):
        raise ParseError("JSONFEED_ITEM_BAD_DATE", f"条目 {index} 的日期不是字符串")
    try:
        published_at = parse_iso_datetime(published_raw)
    except (TypeError, ValueError) as exc:
        raise ParseError("JSONFEED_ITEM_BAD_DATE", f"条目 {index} 的日期无法解析") from exc

    tags = raw.get("tags")
    content = _text(raw.get("content_html")) or _text(raw.get("content_text"))
    return ParsedItem(
        raw_id=_raw_id(raw.get("id")),
        title=normalize_title(_text(raw.get("title"))),
        link=normalize_url(raw.get("url")) or normalize_url(raw.get("external_url")),
        summary=_text(raw.get("summary")),
        content=content,
        published_at=published_at,
        authors=_authors(raw),
        categories=unique_strings(tags if isinstance(tags, list) else ()),
        enclosures=_attachments(raw),
    )


def parse_jsonfeed(body: bytes) -> ParsedFeed:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("PARSE_INVALID_ENCODING", "JSON Feed 必须是 UTF-8") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError("PARSE_INVALID_JSON", f"JSON 解析失败: line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(document, dict):
        raise ParseError("PARSE_JSON_ROOT", "JSON Feed 顶层必须是对象")
    version = document.get("version")
    version_token = version.rstrip("/").rsplit("/", 1)[-1] if isinstance(version, str) else None
    if version_token not in ("1", "1.0", "1.1"):
        raise ParseError("PARSE_UNSUPPORTED_VERSION", f"不支持的 JSON Feed version: {version!r}")
    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        raise ParseError("PARSE_JSON_ITEMS", "items 必须是数组")
    items: list[ParsedItem] = []
    issues: list[ItemIssue] = []
    for index, raw in enumerate(raw_items):
        try:
            items.append(_parse_item(raw, index))
        except ParseError as exc:
            issues.append(ItemIssue(index=index, code=exc.code, message=exc.message))
    return ParsedFeed(format="jsonfeed", items=tuple(items), issues=tuple(issues))
