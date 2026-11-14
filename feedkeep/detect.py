"""Cheap, side-effect-free feed format sniffing."""

from __future__ import annotations

import re
from typing import Optional

ROOT_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(rss|feed)\b", re.IGNORECASE)
CONTENT_TYPE_MAP = {
    "application/rss+xml": "rss",
    "application/atom+xml": "atom",
    "application/feed+json": "jsonfeed",
    "application/json": "jsonfeed",
    "text/xml": None,
}


class FormatDetectionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _hint(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    mime = content_type.split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_MAP.get(mime)


def detect_format(body: bytes, content_type: Optional[str] = None) -> str:
    """Detect rss/atom/jsonfeed without fully parsing the document."""

    if not body or not body.strip():
        raise FormatDetectionError("FORMAT_EMPTY", "响应体为空")
    head = body[:8192]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    stripped = head.lstrip()
    if not stripped:
        raise FormatDetectionError("FORMAT_EMPTY", "响应体为空")
    lowered = stripped[:4096].lower()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        return "jsonfeed"
    match = ROOT_RE.search(lowered)
    if match:
        return "rss" if match.group(1) == b"rss" else "atom"
    hint = _hint(content_type)
    if hint:
        return hint
    if stripped.startswith(b"<"):
        raise FormatDetectionError("FORMAT_UNKNOWN_XML", "XML 根元素不是 RSS rss 或 Atom feed")
    raise FormatDetectionError("FORMAT_UNKNOWN", "无法识别订阅源格式")
