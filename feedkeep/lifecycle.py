"""Pure lifecycle transition planning, kept separate from SQLite side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .identity import changed_fields
from .models import ExistingItem, ItemEvent, NormalizedItem

# Reconciliation semantics offered by ``Store.apply_observation``.
ATOMIC_SNAPSHOT = "atomic"
PROGRESSIVE_VISIBLE = "progressive"
RECONCILE_MODES = (ATOMIC_SNAPSHOT, PROGRESSIVE_VISIBLE)

# Same message as the ledger-level duplicate guard, kept in one place so any
# caller recording a duplicate cannot drift from the user-facing wording.
DUPLICATE_KEY_CODE = "ITEM_DUPLICATE_KEY"
DUPLICATE_KEY_MESSAGE = "条目 {index} 与同一响应中的其他条目身份冲突"


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


def plan_missing_for_hash(content_hash: str) -> ItemEvent:
    """Plan a missing event without materializing the archived item body."""

    return ItemEvent("missing", {"last_content_hash": content_hash})


def stage_observation(items: Sequence[NormalizedItem]) -> list[NormalizedItem]:
    """Bound the in-memory staging set: first key wins, later keys are skipped.

    The staging list only ever holds keys from the current response (at most
    ``max_items_per_feed`` entries); historical item bodies are never loaded.
    Duplicate-key *issues* are the ledger's responsibility, matching the
    historical store boundary where the fetch path reported the conflict and
    the store only applied the first occurrence.
    """

    unique: list[NormalizedItem] = []
    seen: set[str] = set()
    for item in items:
        if item.item_key in seen:
            continue
        seen.add(item.item_key)
        unique.append(item)
    return unique


def validate_reconcile_mode(mode: str) -> None:
    if mode not in RECONCILE_MODES:
        raise ValueError(
            f"unknown reconcile mode {mode!r}; expected one of {RECONCILE_MODES!r}"
        )


def inventory_state(missing_since: Optional[str]) -> str:
    return "missing" if missing_since else "active"


def event_payload(event: ItemEvent) -> dict[str, Any]:
    """Return a JSON-safe, stably ordered payload for persistence."""

    return {"event_type": event.event_type, "detail": dict(event.detail)}
