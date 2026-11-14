from __future__ import annotations

import pytest

from feedkeep.mockdata import generate_all
from feedkeep.parsers import ParseError
from feedkeep.parsers.jsonfeed import parse_jsonfeed


def test_parses_basic_json_feed():
    result = parse_jsonfeed(generate_all()["jsonfeed_basic.json"])
    assert result.format == "jsonfeed"
    assert len(result.items) == 3
    first = result.items[0]
    assert first.raw_id == "jf-1"
    assert first.title == "示例 JSON Feed 条目一"
    assert first.link == "http://127.0.0.1:8765/articles/jf-1"
    assert first.published_at == "2024-01-02T08:00:00Z"
    assert first.authors == ("示例编辑丙",)
    assert first.categories == ("公告", "jsonfeed")
    assert first.enclosures[0].declared_length == 4096
    second = result.items[1]
    assert second.content == "JSON Feed 正文二。"


def test_parses_updated_json_feed():
    result = parse_jsonfeed(generate_all()["jsonfeed_updated.json"])
    assert len(result.items) == 3
    assert result.items[0].title.endswith("（修订）")
    assert result.items[-1].raw_id == "jf-4"


def test_odd_values_are_normalized_or_isolated():
    result = parse_jsonfeed(generate_all()["jsonfeed_odd_values.json"])
    assert len(result.items) == 3
    assert result.items[0].raw_id == "123"
    assert result.items[0].title == "456"
    assert result.items[0].enclosures[0].declared_length == 2048
    assert result.items[0].enclosures[0].mime_type is None
    assert result.items[1].raw_id is None
    assert result.items[1].link == "http://127.0.0.1:8765/articles/odd-2"
    assert result.items[2].raw_id is None
    assert result.items[2].enclosures == ()
    assert [(issue.index, issue.code) for issue in result.issues] == [
        (2, "JSONFEED_ITEM_BAD_ID"),
        (3, "JSONFEED_ITEM_BAD_DATE"),
    ]


@pytest.mark.parametrize("version", [
    "https://jsonfeed.org/version/1.1",
    "1.1",
    "1.0",
    "https://jsonfeed.org/version/1",
])
def test_supported_version_spellings(version):
    body = ('{"version":"%s","items":[]}' % version).encode()
    assert parse_jsonfeed(body).items == ()


def test_unsupported_version_and_bad_json():
    with pytest.raises(ParseError) as raised:
        parse_jsonfeed(b'{"version":"2.0","items":[]}')
    assert raised.value.code == "PARSE_UNSUPPORTED_VERSION"
    with pytest.raises(ParseError) as raised:
        parse_jsonfeed(b'{"version":')
    assert raised.value.code == "PARSE_INVALID_JSON"


def test_non_utf8_and_bad_root():
    with pytest.raises(ParseError) as raised:
        parse_jsonfeed(b"\xff\xfe\x00")
    assert raised.value.code == "PARSE_INVALID_ENCODING"
    with pytest.raises(ParseError) as raised:
        parse_jsonfeed(b"[]")
    assert raised.value.code == "PARSE_JSON_ROOT"


def test_items_must_be_array():
    with pytest.raises(ParseError) as raised:
        parse_jsonfeed(b'{"version":"1.1","items":{}}')
    assert raised.value.code == "PARSE_JSON_ITEMS"
