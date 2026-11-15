from __future__ import annotations

import pytest

from feedkeep.identity import (
    IDENTITY_ID_FIRST,
    IDENTITY_LINK_FIRST,
    IdentityError,
    changed_fields,
    content_hash,
    identify_item,
    normalize_item,
    validate_contract,
)
from feedkeep.models import ExistingItem, ParsedItem


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


# --- explicit identity contracts -------------------------------------

def test_id_first_keeps_historical_precedence():
    item = _parsed()
    identity = identify_item(item, IDENTITY_ID_FIRST)
    assert identity.kind == "id"
    # The default argument must stay backwards compatible.
    assert identify_item(item).item_key == identity.item_key


def test_link_first_prefers_canonical_link_over_publisher_id():
    item = _parsed()
    identity = identify_item(item, IDENTITY_LINK_FIRST)
    assert identity.kind == "link"
    assert identity.item_key.startswith("link:")
    # Same link, rotated publisher id: one continuous identity.
    rotated = identify_item(_parsed(raw_id="urn:test:rotated"), IDENTITY_LINK_FIRST)
    assert rotated.item_key == identity.item_key


def test_id_first_keeps_shared_link_distinct_when_ids_differ():
    first = identify_item(_parsed(raw_id="urn:test:1"), IDENTITY_ID_FIRST)
    second = identify_item(_parsed(raw_id="urn:test:2"), IDENTITY_ID_FIRST)
    assert first.kind == "link" or first.item_key.startswith("id:")
    assert first.item_key != second.item_key
    assert second.item_key.startswith("id:")


def test_link_first_falls_back_to_id_when_link_absent():
    identity = identify_item(_parsed(link=None), IDENTITY_LINK_FIRST)
    assert identity.kind == "id"
    assert identity.item_key.startswith("id:")


def test_both_contracts_fall_back_to_title_date():
    bare = _parsed(raw_id=None, link=None)
    for contract in (IDENTITY_ID_FIRST, IDENTITY_LINK_FIRST):
        identity = identify_item(bare, contract)
        assert identity.kind == "title_date"
        assert identity.item_key.startswith("td:")


def test_title_date_remains_last_resort_when_id_missing_under_link_first():
    no_link = _parsed(raw_id=None, link=None, title="只有标题",
                      published_at="2024-05-01T00:00:00Z")
    identity = identify_item(no_link, IDENTITY_LINK_FIRST)
    assert identity.kind == "title_date"


def test_unknown_contract_is_rejected():
    with pytest.raises(IdentityError) as raised:
        identify_item(_parsed(), "publisher_whim")
    assert raised.value.code == "IDENTITY_BAD_CONTRACT"
    assert validate_contract(IDENTITY_ID_FIRST) == IDENTITY_ID_FIRST
    assert validate_contract(IDENTITY_LINK_FIRST) == IDENTITY_LINK_FIRST


def test_normalize_item_honors_contract_and_keeps_fingerprint():
    id_first = normalize_item(_parsed(), IDENTITY_ID_FIRST)
    link_first = normalize_item(_parsed(), IDENTITY_LINK_FIRST)
    assert id_first.item_key.startswith("id:")
    assert link_first.item_key.startswith("link:")
    # Identity strategy never changes the content fingerprint.
    assert id_first.content_hash == link_first.content_hash
