from __future__ import annotations

from feedkeep.lifecycle import (
    inventory_state,
    plan_identity_change,
    plan_missing,
    plan_transition,
)
from feedkeep.models import ExistingItem


def _existing(item, missing_since=None, **overrides):
    values = dict(
        source_id="s", item_key=item.item_key, title=item.title, link=item.link,
        summary=item.summary, content=item.content, published_at=item.published_at,
        authors=item.authors, categories=item.categories,
        enclosures=item.enclosures, content_hash=item.content_hash,
        missing_since=missing_since,
    )
    values.update(overrides)
    return ExistingItem(**values)


def test_new_item_is_discovered(item_factory):
    item = item_factory()
    plan = plan_transition(None, item)
    assert plan.is_new and not plan.is_updated and not plan.is_restored
    assert [event.event_type for event in plan.events] == ["discovered"]
    assert plan.events[0].detail["content_hash"] == item.content_hash


def test_unchanged_item_emits_nothing(item_factory):
    item = item_factory()
    plan = plan_transition(_existing(item), item)
    assert not plan.is_new and not plan.is_updated and not plan.is_restored
    assert plan.events == ()


def test_updated_item_records_changed_fields(item_factory):
    item = item_factory(title="新标题", summary="新摘要")
    old = _existing(item, title="旧标题", summary="旧摘要", content_hash="old-hash")
    plan = plan_transition(old, item)
    assert plan.is_updated and not plan.is_restored
    event = plan.events[0]
    assert event.event_type == "updated"
    assert event.detail["changed_fields"] == ["summary", "title"]
    assert event.detail["previous_hash"] == "old-hash"


def test_missing_item_restores(item_factory):
    item = item_factory()
    plan = plan_transition(_existing(item, missing_since="2024-01-02T00:00:00Z"), item)
    assert plan.is_restored and not plan.is_updated
    assert [event.event_type for event in plan.events] == ["restored"]


def test_missing_item_can_be_restored_and_updated(item_factory):
    item = item_factory(content="new content")
    old = _existing(item, content=None, missing_since="2024-01-02T00:00:00Z",
                    content_hash="old")
    plan = plan_transition(old, item)
    assert plan.is_restored and plan.is_updated
    assert [event.event_type for event in plan.events] == ["restored", "updated"]


def test_plan_missing_and_inventory_state(item_factory):
    item = item_factory()
    event = plan_missing(_existing(item))
    assert event.event_type == "missing"
    assert event.detail["last_content_hash"] == item.content_hash
    assert inventory_state(None) == "active"
    assert inventory_state("2024-01-02T00:00:00Z") == "missing"


def test_plan_identity_change_records_both_keys_and_contracts(item_factory):
    item = item_factory()
    event = plan_identity_change(
        old_item_key=item.item_key,
        new_item_key="link:0123456789abcdef0123456789abcdef",
        from_contract="id_first",
        to_contract="link_first",
        identity_kind="link",
    )
    assert event.event_type == "identity_changed"
    assert event.detail == {
        "from_contract": "id_first",
        "to_contract": "link_first",
        "old_item_key": item.item_key,
        "new_item_key": "link:0123456789abcdef0123456789abcdef",
        "identity_kind": "link",
    }
    # Identity changes never feed back into the content state machine.
    plan = plan_transition(None, item)
    assert plan.is_new and [e.event_type for e in plan.events] == ["discovered"]
