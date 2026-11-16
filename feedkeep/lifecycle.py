"""Pure lifecycle transition planning, kept separate from SQLite side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .clock import parse_utc
from .identity import changed_fields
from .models import (
    DEFAULT_REVISION_ORDER,
    REVISION_SOURCE_ATOM_UPDATED,
    ExistingItem,
    ItemEvent,
    NormalizedItem,
)


@dataclass(frozen=True)
class Transition:
    is_new: bool
    is_updated: bool
    is_restored: bool
    events: tuple[ItemEvent, ...]
    # False when a source_revision_order gate rejects the incoming projection
    # (stale or conflict): the current items row keeps its content.
    accept_content: bool = True
    # True when this observation establishes the Atom watermark on a legacy
    # row whose pre-upgrade revision order was unknown.
    revision_baseline_established: bool = False


def plan_transition(existing: Optional[ExistingItem], item: NormalizedItem) -> Transition:
    """Decide discovered/updated/restored/unchanged for one observed item.

    This is the legacy observation-order planner and remains the exact
    behaviour used by the default ``observed_order`` strategy.
    """

    if existing is None:
        event = ItemEvent("discovered", {"content_hash": item.content_hash})
        return Transition(True, False, False, (event,))

    is_updated = existing.content_hash != item.content_hash
    is_restored = existing.missing_since is not None
    events: list[ItemEvent] = []
    if is_restored:
        events.append(ItemEvent("restored", {"content_hash": item.content_hash}))
    if is_updated:
        fields_ = changed_fields(existing, item)
        events.append(ItemEvent("updated", {
            "content_hash": item.content_hash,
            "previous_hash": existing.content_hash,
            "changed_fields": fields_,
        }))
    return Transition(False, is_updated, is_restored, tuple(events))


def _revision_key(value: Optional[str]) -> tuple[int, Any]:
    """Comparable key for canonical UTC stamps; None sorts below any instant."""

    if not value:
        return (0, 0)
    return (1, parse_utc(value))


def plan_observation(
    existing: Optional[ExistingItem],
    item: NormalizedItem,
    revision_order: str = DEFAULT_REVISION_ORDER,
) -> Transition:
    """Plan one observation under the source's explicit revision strategy.

    ``observed_order`` (default) delegates to :func:`plan_transition`, so a
    complete valid response always becomes the current projection, even when
    its Atom ``updated`` moves backwards.

    ``source_revision_order`` treats Atom ``updated`` as a per-item monotonic
    high-water mark. Items without an Atom revision stamp (RSS / JSON Feed,
    or legacy rows before their first baseline) keep observation-order
    semantics, because no source revision order can be evaluated for them.
    """

    if revision_order != "source_revision_order" or existing is None:
        return plan_transition(existing, item)
    if not item.updated_at:
        return plan_transition(existing, item)

    restored = existing.missing_since is not None
    restored_events: list[ItemEvent] = [
        ItemEvent("restored", {"content_hash": item.content_hash})
    ] if restored else []

    # Legacy rows carry no watermark: the revision order before the strategy
    # was enabled is unknown, so the first valid Atom observation establishes
    # the baseline unconditionally instead of fabricating a stale event.
    if not existing.revision_updated_at:
        legacy = plan_transition(existing, item)
        return Transition(
            is_new=legacy.is_new,
            is_updated=legacy.is_updated,
            is_restored=legacy.is_restored,
            events=legacy.events,
            accept_content=True,
            revision_baseline_established=True,
        )

    incoming_key = _revision_key(item.updated_at)
    watermark_key = _revision_key(existing.revision_updated_at)

    if incoming_key < watermark_key:
        # Stale mirror/rollback: keep the current content, but still record
        # the observation evidence (its stamp, hash and originating run is
        # attached by the store when the event is persisted).
        event = ItemEvent("stale_revision", {
            "content_hash": item.content_hash,
            "current_hash": existing.content_hash,
            "incoming_updated_at": item.updated_at,
            "reason": "incoming_updated_before_revision_watermark",
            "revision_source": REVISION_SOURCE_ATOM_UPDATED,
            "watermark_updated_at": existing.revision_updated_at,
        })
        return Transition(
            False, False, restored, tuple(restored_events + [event]),
            accept_content=False,
        )

    if incoming_key == watermark_key:
        if existing.content_hash == item.content_hash:
            # Same revision, same content: pure idempotency. Restore still
            # applies when the item had previously been marked missing.
            return Transition(False, False, restored, tuple(restored_events))
        # Same revision stamp with divergent content is an unordered conflict;
        # neither side is silently preferred.
        event = ItemEvent("revision_conflict", {
            "content_hash": item.content_hash,
            "current_hash": existing.content_hash,
            "incoming_updated_at": item.updated_at,
            "reason": "same_updated_at_different_content",
            "revision_source": REVISION_SOURCE_ATOM_UPDATED,
            "watermark_updated_at": existing.revision_updated_at,
        })
        return Transition(
            False, False, restored, tuple(restored_events + [event]),
            accept_content=False,
        )

    # A later source revision: accept via the ordinary observation planner,
    # which also computes changed_fields / restored events.
    return plan_transition(existing, item)


def plan_missing(existing: ExistingItem) -> ItemEvent:
    return ItemEvent("missing", {"last_content_hash": existing.content_hash})


def inventory_state(missing_since: Optional[str]) -> str:
    return "missing" if missing_since else "active"


def event_payload(event: ItemEvent) -> dict[str, Any]:
    """Return a JSON-safe, stably ordered payload for persistence."""

    return {"event_type": event.event_type, "detail": dict(event.detail)}
