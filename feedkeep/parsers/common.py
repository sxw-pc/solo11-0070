"""Shared, dependency-free parsing and normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET


class ParseError(Exception):
    """A whole-document failure with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class UnsafeXML(ParseError):
    pass


_WS_RE = re.compile(r"\s+")
_DATE_PREFIX_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_text(value: Optional[str], collapse: bool = False) -> Optional[str]:
    """NFC-normalize text, normalize newlines and trim the result."""

    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    if collapse:
        text = _WS_RE.sub(" ", text)
    return text or None


def normalize_title(value: Optional[str]) -> Optional[str]:
    return normalize_text(value, collapse=True)


def normalize_url(value: Optional[str]) -> Optional[str]:
    """Normalize an item URL; return None for non-http(s) or malformed values."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(ord(c) < 32 for c in raw) or "\\" in raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if port is not None and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def parse_datetime(value: Optional[str], style: str = "iso") -> Optional[str]:
    """Parse RFC-822 or ISO-8601 and return canonical UTC seconds.

    Raises ValueError for a non-empty but invalid value. An absent value
    returns None. The caller decides whether that is an item-level issue.
    """

    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    if style == "rfc822":
        parsed = parsedate_to_datetime(raw)
    else:
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        # Accept a date-only value, common in hand-written feeds.
        if _DATE_PREFIX_RE.match(raw) and "T" not in raw and " " not in raw:
            parsed = datetime.fromisoformat(raw + "T00:00:00+00:00")
        else:
            parsed = datetime.fromisoformat(raw)
    if parsed is None:
        raise ValueError("empty date")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_datetime(value: Optional[str]) -> Optional[str]:
    return parse_datetime(value, style="iso")


def parse_rfc822_datetime(value: Optional[str]) -> Optional[str]:
    return parse_datetime(value, style="rfc822")


def parse_declared_length(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def parse_xml_document(body: bytes) -> ET.Element:
    """Parse XML while explicitly rejecting DTD/entity declarations."""

    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise UnsafeXML("PARSE_UNSAFE_XML", "拒绝包含 DOCTYPE/ENTITY 的 XML")
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise ParseError("PARSE_INVALID_XML", f"XML 解析失败: {exc}") from exc


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if local_name(child.tag) == name]


def child(element: ET.Element, name: str) -> Optional[ET.Element]:
    for candidate in list(element):
        if local_name(candidate.tag) == name:
            return candidate
    return None


def child_text(element: ET.Element, name: str) -> Optional[str]:
    found = child(element, name)
    return element_text(found) if found is not None else None


def element_text(element: Optional[ET.Element]) -> Optional[str]:
    if element is None:
        return None
    text = "".join(element.itertext())
    return normalize_text(text)


def first_nonempty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = normalize_title(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def require_root(root: ET.Element, expected: str) -> None:
    if local_name(root.tag).lower() != expected:
        raise ParseError(
            "PARSE_FORMAT_MISMATCH",
            f"根元素是 {local_name(root.tag)!r}，期望 {expected!r}",
        )
