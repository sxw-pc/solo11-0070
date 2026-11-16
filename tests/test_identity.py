from __future__ import annotations

import pytest

from feedkeep.identity import (
    IdentityError,
    changed_fields,
    content_hash,
    identify_item,
    identify_tombstone,
    normalize_item,
)
from feedkeep.models import ExistingItem, ParsedItem, ParsedTombstone


def _parsed(**overrides):
    base = dict(
        raw_id="urn:test:1", title="标题", link="http://127.0.0.1:8765/a",
        summary=None, content=None, published_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return ParsedItem(**base)


def test_identity_precedence_uses_explicit_id():
    item = _parsed()
    identity = identify_item(item)
    assert identity.kind == "id"
    assert identity.item_key.startswith("id:")
    assert len(identity.item_key) == 35


def test_link_and_title_date_fallbacks_are_stable():
    linked = _parsed(raw_id=None)
    first = identify_item(linked)
    second = identify_item(linked)
    assert first.kind == "link"
    assert first.item_key == second.item_key

    title_date = _parsed(raw_id=None, link=None)
    fallback = identify_item(title_date)
    assert fallback.kind == "title_date"
    assert fallback.item_key == identify_item(title_date).item_key
    assert fallback.item_key.startswith("td:")


def test_identity_missing_everything_fails():
    with pytest.raises(IdentityError) as raised:
        identify_item(_parsed(raw_id=None, link=None, title=None))
    assert raised.value.code == "ITEM_NO_IDENTITY"


def test_content_hash_changes_only_with_archived_content():
    item = _parsed()
    baseline = content_hash(item)
    # Summary is archived content, so it changes the fingerprint.
    assert baseline != content_hash(_parsed(summary="different summary"))
    # Identity metadata is not content.
    assert content_hash(_parsed()) == content_hash(_parsed(raw_id="different-id"))


def test_normalize_item_trims_and_fingerprints():
    normalized = normalize_item(_parsed(title="  A\n B  ", summary=" x\r\ny "))
    assert normalized.title == "A B"
    assert normalized.summary == "x\ny"
    assert normalized.item_key.startswith("id:")


def test_changed_fields_are_sorted():
    normalized = normalize_item(_parsed(summary="new", content="body"))
    existing = ExistingItem(
        source_id="s", item_key=normalized.item_key,
        title="旧标题", link=normalized.link, summary="old", content=None,
        published_at=normalized.published_at, authors=(), categories=(),
        enclosures=(), content_hash="old", missing_since=None,
    )
    assert changed_fields(existing, normalized) == ["content", "summary", "title"]


def test_tombstone_ref_uses_same_key_as_explicit_item_id():
    item_identity = identify_item(_parsed(raw_id="urn:feedkeep:atom:alpha"))
    marker = identify_tombstone(
        ParsedTombstone(raw_ref="urn:feedkeep:atom:alpha",
                        declared_at="2024-02-01T00:00:00Z")
    )
    assert marker.item_key == item_identity.item_key
    assert marker.raw_ref == "urn:feedkeep:atom:alpha"
    assert marker.declared_at == "2024-02-01T00:00:00Z"


def test_tombstone_ref_is_whitespace_normalized():
    marker = identify_tombstone(
        ParsedTombstone(raw_ref="  urn:feedkeep:atom:alpha\n",
                        declared_at="2024-02-01T00:00:00Z")
    )
    assert marker.raw_ref == "urn:feedkeep:atom:alpha"
    assert marker.item_key == identify_item(
        _parsed(raw_id="urn:feedkeep:atom:alpha")
    ).item_key


def test_tombstone_without_usable_ref_fails():
    with pytest.raises(IdentityError) as raised:
        identify_tombstone(
            ParsedTombstone(raw_ref="   ", declared_at="2024-02-01T00:00:00Z")
        )
    assert raised.value.code == "TOMBSTONE_NO_REF"
