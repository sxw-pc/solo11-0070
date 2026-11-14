"""Format parser registry."""

from __future__ import annotations

from ..models import ParsedFeed
from .atom import parse_atom
from .common import ParseError, UnsafeXML
from .jsonfeed import parse_jsonfeed
from .rss import parse_rss

PARSERS = {
    "rss": parse_rss,
    "atom": parse_atom,
    "jsonfeed": parse_jsonfeed,
}


def parse_feed(fmt: str, body: bytes) -> ParsedFeed:
    parser = PARSERS.get(fmt)
    if parser is None:
        raise ParseError("PARSE_FORMAT_UNKNOWN", f"未知格式: {fmt!r}")
    return parser(body)


__all__ = ["PARSERS", "ParseError", "UnsafeXML", "parse_feed"]
