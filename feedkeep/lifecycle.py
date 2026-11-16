"""Pure lifecycle transition planning, kept separate from SQLite side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .identity import changed_fields
from .models import ExistingItem, ItemEvent, NormalizedItem, NormalizedTombstone


@dataclass(frozen=True)
class Transition:
    is_new: bool
    is_updated: bool
    is_restored: bool
    events: tuple[ItemEvent, ...]


def plan_transition(existing: Optional[ExistingItem], item: NormalizedItem) -> Transition:
    """Decide discovered/updated/restored/unchanged for one observed item."""

    if existing is None:
        event = ItemEvent("discovered", {"content_hash": item.content_hash})
        return Transition(True, False, False, (event,))

    is_updated = existing.content_hash != item.content_hash
    # Only an ordinary absence (missing_since) is a "restored" revival.
    # Retracted entries that reappear return to active inventory without a
    # restored event: a publisher retraction is not an observation gap.
    is_restored = existing.missing_since is not None
    events: list[ItemEvent] = []
    if is_restored:
        events.append(ItemEvent("restored", {"content_hash": item.content_hash}))
    if is_updated:
        fields = changed_fields(existing, item)
        events.append(ItemEvent("updated", {
            "content_hash": item.content_hash,
            "previous_hash": existing.content_hash,
            "changed_fields": fields,
        }))
    return Transition(False, is_updated, is_restored, tuple(events))


def plan_missing(existing: ExistingItem) -> ItemEvent:
    return ItemEvent("missing", {"last_content_hash": existing.content_hash})


@dataclass(frozen=True)
class RetractionDecision:
    """Outcome of evaluating one tombstone against the current inventory."""

    accepted: bool
    stale: bool
    event: Optional[ItemEvent] = None


def plan_retraction(existing: Optional[ExistingItem],
                    tombstone: NormalizedTombstone,
                    *, live_update: Optional[str] = None) -> RetractionDecision:
    """Decide whether a publisher deletion marker may retract an item.

    The marker's declared time is compared against the newest fact known about
    the entry (the item's own ``updated_at`` when present, otherwise the
    archive's ``last_changed_at``). A marker older than that fact is stale and
    must never withdraw newer content, regardless of XML ordering.

    ``live_update`` carries the ``updated_at`` of an entry appearing in the
    same response as the tombstone but not yet archived, so the conflict can
    still be resolved by event time on a first observation.
    """

    if existing is None:
        if live_update is not None and tombstone.declared_at < live_update:
            return RetractionDecision(False, True)
        # No archived entry backs this ref: there is nothing to retract.
        return RetractionDecision(False, False)
    if existing.retracted_at is not None:
        # Repeated notification of the same deletion: idempotent no-op.
        return RetractionDecision(False, False)
    newest_at = live_update or existing.updated_at or existing.last_changed_at
    if newest_at is not None and tombstone.declared_at < newest_at:
        return RetractionDecision(False, True)
    event = ItemEvent("retracted", {
        "ref": tombstone.raw_ref,
        "declared_at": tombstone.declared_at,
        "last_content_hash": existing.content_hash,
    })
    return RetractionDecision(True, False, event)


def inventory_state(missing_since: Optional[str],
                    retracted_at: Optional[str] = None) -> str:
    if retracted_at is not None:
        return "retracted"
    return "missing" if missing_since else "active"


def event_payload(event: ItemEvent) -> dict[str, Any]:
    """Return a JSON-safe, stably ordered payload for persistence."""

    return {"event_type": event.event_type, "detail": dict(event.detail)}
