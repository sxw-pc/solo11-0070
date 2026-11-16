from __future__ import annotations

from feedkeep.lifecycle import (inventory_state, plan_missing, plan_retraction,
                                plan_transition)
from feedkeep.models import ExistingItem, NormalizedTombstone


def _tombstone(item, when="2024-02-01T00:00:00Z"):
    return NormalizedTombstone(
        item_key=item.item_key, raw_ref=item.raw_id, declared_at=when,
    )


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
    assert inventory_state(None, "2024-02-01T00:00:00Z") == "retracted"
    # Retraction wins over a missing timestamp when both are somehow present.
    assert inventory_state("2024-01-02T00:00:00Z", "2024-02-01T00:00:00Z") == "retracted"


def test_retraction_accepted_for_known_active_item(item_factory):
    item = item_factory()
    decision = plan_retraction(_existing(item), _tombstone(item))
    assert decision.accepted and not decision.stale
    assert decision.event.event_type == "retracted"
    assert decision.event.detail == {
        "ref": item.raw_id,
        "declared_at": "2024-02-01T00:00:00Z",
        "last_content_hash": item.content_hash,
    }


def test_retraction_unknown_ref_is_noop(item_factory):
    item = item_factory()
    decision = plan_retraction(None, _tombstone(item))
    assert not decision.accepted and not decision.stale
    assert decision.event is None


def test_retraction_already_retracted_is_idempotent(item_factory):
    item = item_factory()
    existing = _existing(item, retracted_at="2024-02-01T00:00:00Z")
    decision = plan_retraction(existing, _tombstone(item))
    assert not decision.accepted and not decision.stale


def test_stale_tombstone_rejected_by_updated_at(item_factory):
    item = item_factory()
    # A newer entry (updated 2024-02-10) versus an old deletion (when 2024-02-01).
    existing = _existing(item, updated_at="2024-02-10T00:00:00Z",
                         last_changed_at="2024-02-10T00:00:00Z")
    decision = plan_retraction(existing, _tombstone(item, "2024-02-01T00:00:00Z"))
    assert not decision.accepted and decision.stale


def test_stale_tombstone_falls_back_to_last_changed_at(item_factory):
    item = item_factory()
    existing = _existing(item, updated_at=None,
                         last_changed_at="2024-02-10T00:00:00Z")
    decision = plan_retraction(existing, _tombstone(item, "2024-02-01T00:00:00Z"))
    assert not decision.accepted and decision.stale


def test_tombstone_equal_to_item_time_is_not_stale(item_factory):
    item = item_factory()
    existing = _existing(item, updated_at="2024-02-01T00:00:00Z")
    decision = plan_retraction(existing, _tombstone(item, "2024-02-01T00:00:00Z"))
    assert decision.accepted and not decision.stale


def test_retraction_also_covers_missing_item(item_factory):
    item = item_factory()
    existing = _existing(item, missing_since="2024-01-20T00:00:00Z")
    decision = plan_retraction(existing, _tombstone(item))
    assert decision.accepted and not decision.stale


def test_reappearing_retracted_item_is_not_a_restored_event(item_factory):
    item = item_factory()
    existing = _existing(item, retracted_at="2024-02-01T00:00:00Z")
    plan = plan_transition(existing, item)
    assert not plan.is_restored
    assert plan.events == ()
