from __future__ import annotations

import pytest

from feedkeep.mockdata import generate_all
from feedkeep.parsers import ParseError
from feedkeep.parsers.common import UnsafeXML
from feedkeep.parsers.rss import parse_rss


def test_parses_basic_rss():
    result = parse_rss(generate_all()["rss_basic.xml"])
    assert result.format == "rss"
    assert len(result.items) == 3
    assert result.issues == ()
    first = result.items[0]
    assert first.raw_id == "urn:feedkeep:rss-basic:1"
    assert first.title == "示例公告一：发布窗口"
    assert first.link == "http://127.0.0.1:8765/articles/rss-1"
    assert first.published_at == "2024-01-01T08:00:00Z"
    assert first.summary == "第一条合成公告的摘要。"
    assert first.content == "<p>正文一，仅用于离线测试。</p>"
    assert first.authors == ("示例编辑甲",)
    assert first.categories == ("公告",)
    assert first.enclosures[0].mime_type == "application/pdf"
    assert first.enclosures[0].declared_length == 1024
    assert first.enclosures[0].position == 0


def test_parses_updated_rss():
    result = parse_rss(generate_all()["rss_updated.xml"])
    assert len(result.items) == 4
    assert result.items[0].title.endswith("（修订）")
    assert any(item.raw_id.endswith(":4") for item in result.items)


def test_malformed_document_fails_whole_source():
    with pytest.raises(ParseError) as raised:
        parse_rss(generate_all()["rss_malformed.xml"])
    assert raised.value.code == "PARSE_INVALID_XML"


def test_rejects_doctype_and_entity():
    with pytest.raises(UnsafeXML) as raised:
        parse_rss(b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>')
    assert raised.value.code == "PARSE_UNSAFE_XML"


def test_bad_item_date_is_isolated():
    body = b"""<rss version="2.0"><channel>
      <item><guid>bad</guid><title>bad</title><pubDate>not-a-date</pubDate></item>
      <item><guid>good</guid><title>good</title><pubDate>Tue, 02 Jan 2024 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    result = parse_rss(body)
    assert len(result.items) == 1
    assert result.items[0].raw_id == "good"
    assert [(issue.index, issue.code) for issue in result.issues] == [(0, "RSS_ITEM_BAD_DATE")]


def test_missing_guid_is_not_a_parser_error():
    body = b"""<rss version="2.0"><channel><item>
      <title>no guid</title><link>http://127.0.0.1:8765/a</link>
      <pubDate>Tue, 02 Jan 2024 00:00:00 GMT</pubDate>
    </item></channel></rss>"""
    result = parse_rss(body)
    assert result.items[0].raw_id is None
    assert result.issues == ()


def test_wrong_root_is_a_format_error():
    with pytest.raises(ParseError) as raised:
        parse_rss(generate_all()["atom_basic.xml"])
    assert raised.value.code == "PARSE_FORMAT_MISMATCH"
