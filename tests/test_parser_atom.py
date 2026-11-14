from __future__ import annotations

import pytest

from feedkeep.mockdata import generate_all
from feedkeep.parsers import ParseError
from feedkeep.parsers.atom import parse_atom


def test_parses_basic_atom():
    result = parse_atom(generate_all()["atom_basic.xml"])
    assert result.format == "atom"
    assert len(result.items) == 3
    first = result.items[0]
    assert first.raw_id == "urn:feedkeep:atom:alpha"
    assert first.title == "示例 Atom 条目甲"
    assert first.link == "http://127.0.0.1:8765/articles/atom-alpha"
    assert first.published_at == "2024-01-01T08:00:00Z"
    assert first.authors == ("示例编辑乙",)
    assert first.categories == ("公告",)
    assert first.enclosures[0].url.endswith("atom-alpha.pdf")
    assert first.enclosures[0].declared_length == 2048


def test_atom_falls_back_to_updated_when_published_missing():
    result = parse_atom(generate_all()["atom_basic.xml"])
    beta = next(item for item in result.items if item.raw_id.endswith(":beta"))
    assert beta.published_at == "2024-01-03T10:30:00Z"


def test_updated_atom_contains_new_entry():
    result = parse_atom(generate_all()["atom_updated.xml"])
    assert len(result.items) == 3
    assert any(item.raw_id.endswith(":delta") for item in result.items)
    assert not any(item.raw_id.endswith(":gamma") for item in result.items)


def test_mixed_feed_isolates_three_bad_entries():
    result = parse_atom(generate_all()["atom_mixed_bad_items.xml"])
    assert len(result.items) == 2
    assert [item.raw_id for item in result.items] == [
        "urn:feedkeep:mixed:good-1", "urn:feedkeep:mixed:good-2",
    ]
    assert [(issue.index, issue.code) for issue in result.issues] == [
        (1, "ATOM_ITEM_MISSING_ID"),
        (3, "ATOM_ITEM_MISSING_TIME"),
        (4, "ATOM_ITEM_BAD_DATE"),
    ]


def test_entry_without_id_is_item_error():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <title>t</title><id>urn:f</id>
      <entry><title>x</title><updated>2024-01-01T00:00:00Z</updated></entry>
    </feed>"""
    result = parse_atom(body)
    assert result.items == ()
    assert result.issues[0].code == "ATOM_ITEM_MISSING_ID"


def test_wrong_root_rejected():
    with pytest.raises(ParseError) as raised:
        parse_atom(generate_all()["rss_basic.xml"])
    assert raised.value.code == "PARSE_FORMAT_MISMATCH"
