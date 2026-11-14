"""RSS 2.0 parser with item-level fault isolation."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..models import Enclosure, ItemIssue, ParsedFeed, ParsedItem
from .common import (
    ParseError,
    child,
    child_text,
    children,
    element_text,
    normalize_title,
    normalize_url,
    parse_declared_length,
    parse_rfc822_datetime,
    parse_xml_document,
    require_root,
    unique_strings,
)


def _enclosures(item: ET.Element) -> tuple[Enclosure, ...]:
    found: list[Enclosure] = []
    for index, node in enumerate(children(item, "enclosure")):
        url = normalize_url(node.get("url"))
        if not url:
            continue
        mime = (node.get("type") or "").strip() or None
        length = parse_declared_length(node.get("length"))
        found.append(Enclosure(url, mime, length, len(found)))
    return tuple(found)


def _parse_item(item: ET.Element, index: int) -> ParsedItem:
    published = child_text(item, "pubDate")
    try:
        published_at = parse_rfc822_datetime(published)
    except (TypeError, ValueError) as exc:
        raise ParseError("RSS_ITEM_BAD_DATE", f"条目 {index} 的 pubDate 无法解析") from exc

    raw_id = child_text(item, "guid")
    author_nodes = children(item, "author") + children(item, "creator")
    authors = unique_strings(element_text(node) for node in author_nodes)
    categories = unique_strings(element_text(node) for node in children(item, "category"))
    summary = child_text(item, "description")
    content = child_text(item, "encoded")
    return ParsedItem(
        raw_id=raw_id,
        title=normalize_title(child_text(item, "title")),
        link=normalize_url(child_text(item, "link")),
        summary=summary,
        content=content,
        published_at=published_at,
        authors=authors,
        categories=categories,
        enclosures=_enclosures(item),
    )


def parse_rss(body: bytes) -> ParsedFeed:
    root = parse_xml_document(body)
    require_root(root, "rss")
    channel = child(root, "channel")
    if channel is None:
        raise ParseError("PARSE_RSS_NO_CHANNEL", "RSS 缺少 channel 元素")
    items: list[ParsedItem] = []
    issues: list[ItemIssue] = []
    for index, item in enumerate(children(channel, "item")):
        try:
            items.append(_parse_item(item, index))
        except ParseError as exc:
            issues.append(ItemIssue(index=index, code=exc.code, message=exc.message))
    return ParsedFeed(format="rss", items=tuple(items), issues=tuple(issues))
