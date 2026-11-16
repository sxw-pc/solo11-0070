"""Atom 1.0 parser with item-level fault isolation."""

from __future__ import annotations

from typing import Optional
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
    parse_iso_datetime,
    parse_xml_document,
    require_root,
    unique_strings,
)


def _entry_link(entry: ET.Element) -> Optional[str]:
    candidates = children(entry, "link")
    for node in candidates:
        rel = (node.get("rel") or "alternate").strip().lower()
        if rel == "alternate" and node.get("href"):
            return normalize_url(node.get("href"))
    for node in candidates:
        if node.get("href") and (node.get("rel") or "").strip().lower() not in {"self", "enclosure"}:
            return normalize_url(node.get("href"))
    return None


def _enclosures(entry: ET.Element) -> tuple[Enclosure, ...]:
    found: list[Enclosure] = []
    for node in children(entry, "link"):
        if (node.get("rel") or "").strip().lower() != "enclosure":
            continue
        url = normalize_url(node.get("href"))
        if not url:
            continue
        mime = (node.get("type") or "").strip() or None
        length = parse_declared_length(node.get("length"))
        found.append(Enclosure(url, mime, length, len(found)))
    return tuple(found)


def _authors(entry: ET.Element) -> tuple[str, ...]:
    values: list[str] = []
    for author in children(entry, "author"):
        name = child_text(author, "name")
        values.append(name or element_text(author) or "")
    return unique_strings(values)


def _parse_entry(entry: ET.Element, index: int) -> ParsedItem:
    raw_id = child_text(entry, "id")
    if not raw_id:
        raise ParseError("ATOM_ITEM_MISSING_ID", f"条目 {index} 缺少 id")
    updated_raw = child_text(entry, "updated")
    if not updated_raw:
        raise ParseError("ATOM_ITEM_MISSING_TIME", f"条目 {index} 缺少 updated")
    published_raw = child_text(entry, "published")
    try:
        updated = parse_iso_datetime(updated_raw)
        published = parse_iso_datetime(published_raw)
    except (TypeError, ValueError) as exc:
        raise ParseError("ATOM_ITEM_BAD_DATE", f"条目 {index} 的时间无法解析") from exc

    authors = _authors(entry)
    categories = unique_strings(
        node.get("term") or node.get("label") or element_text(node)
        for node in children(entry, "category")
    )
    return ParsedItem(
        raw_id=raw_id,
        title=normalize_title(child_text(entry, "title")),
        link=_entry_link(entry),
        summary=child_text(entry, "summary"),
        content=child_text(entry, "content"),
        published_at=published or updated,
        authors=authors,
        categories=categories,
        enclosures=_enclosures(entry),
        # Keep the source revision stamp separately so the store can protect
        # the current projection against mirror rollback; both are UTC-normalized.
        updated_at=updated,
    )


def parse_atom(body: bytes) -> ParsedFeed:
    root = parse_xml_document(body)
    require_root(root, "feed")
    items: list[ParsedItem] = []
    issues: list[ItemIssue] = []
    for index, entry in enumerate(children(root, "entry")):
        try:
            items.append(_parse_entry(entry, index))
        except ParseError as exc:
            issues.append(ItemIssue(index=index, code=exc.code, message=exc.message))
    return ParsedFeed(format="atom", items=tuple(items), issues=tuple(issues))
