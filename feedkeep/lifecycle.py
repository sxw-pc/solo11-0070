"""Pure lifecycle transition planning, kept separate from SQLite side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .identity import changed_fields
from .models import ExistingItem, ItemEvent, NormalizedItem


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


def plan_identity_change(old_item_key: str, new_item_key: str,
                         from_contract: str, to_contract: str,
                         identity_kind: str) -> ItemEvent:
    """Build the audit event appended when a contract migration renames a key.

    The event lives on the new key and preserves the old key inside its
    detail, so the full event chain stays traceable across the rename.
    """

    return ItemEvent("identity_changed", {
        "from_contract": from_contract,
        "to_contract": to_contract,
        "old_item_key": old_item_key,
        "new_item_key": new_item_key,
        "identity_kind": identity_kind,
    })


def inventory_state(missing_since: Optional[str]) -> str:
    return "missing" if missing_since else "active"


def event_payload(event: ItemEvent) -> dict[str, Any]:
    """Return a JSON-safe, stably ordered payload for persistence."""

    return {"event_type": event.event_type, "detail": dict(event.detail)}
