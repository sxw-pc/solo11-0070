"""Stable item identity and canonical content fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .models import NormalizedItem, ParsedItem
from .parsers.common import normalize_text, normalize_title, normalize_url


class IdentityError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


#: 发布者 ID 优先：显式 id -> 规范链接 -> title+date（历史默认行为）。
IDENTITY_ID_FIRST = "id_first"
#: 规范链接优先：规范链接 -> 显式 id -> title+date。
IDENTITY_LINK_FIRST = "link_first"
IDENTITY_CONTRACTS = (IDENTITY_ID_FIRST, IDENTITY_LINK_FIRST)
IDENTITY_DEFAULT = IDENTITY_ID_FIRST


def validate_contract(contract: str) -> str:
    if contract not in IDENTITY_CONTRACTS:
        raise IdentityError(
            "IDENTITY_BAD_CONTRACT",
            f"未知身份契约 {contract!r}，必须是 {IDENTITY_CONTRACTS!r} 之一",
        )
    return contract


@dataclass(frozen=True)
class ItemIdentity:
    kind: str
    value: str
    item_key: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _title_date_identity(item: ParsedItem) -> Optional[ItemIdentity]:
    title = normalize_title(item.title)
    published = normalize_text(item.published_at)
    if title and published:
        value = f"{title.casefold()}\x1f{published}"
        return ItemIdentity("title_date", value, "td:" + _digest(value))
    return None


def _id_identity(item: ParsedItem) -> Optional[ItemIdentity]:
    raw_id = normalize_text(item.raw_id, collapse=True)
    if raw_id:
        return ItemIdentity("id", raw_id, "id:" + _digest(raw_id))
    return None


def _link_identity(item: ParsedItem) -> Optional[ItemIdentity]:
    link = normalize_url(item.link)
    if link:
        return ItemIdentity("link", link, "link:" + _digest(link))
    return None


def identify_item(item: ParsedItem, contract: str = IDENTITY_DEFAULT) -> ItemIdentity:
    """Assign item identity under the source's declared contract.

    ``id_first`` keeps the historical precedence: explicit id, canonical link,
    then title+date.  ``link_first`` swaps the first two routes so an article
    keeps one continuous history when the publisher rotates its id.  Both
    contracts fall back to title+date when the two primary fields are absent.
    """

    validate_contract(contract)
    if contract == IDENTITY_LINK_FIRST:
        primary = (_link_identity, _id_identity)
    else:
        primary = (_id_identity, _link_identity)
    for resolver in primary:
        identity = resolver(item)
        if identity is not None:
            return identity
    fallback = _title_date_identity(item)
    if fallback is not None:
        return fallback
    raise IdentityError("ITEM_NO_IDENTITY", "条目缺少可用身份（id / link / title+date）")


def canonical_content(item: ParsedItem) -> str:
    payload: dict[str, Any] = {
        "title": normalize_title(item.title),
        "link": normalize_url(item.link),
        "summary": normalize_text(item.summary),
        "content": normalize_text(item.content),
        "published_at": normalize_text(item.published_at),
        "authors": [normalize_title(author) for author in item.authors],
        "categories": [normalize_title(category) for category in item.categories],
        "enclosures": [enclosure.as_dict() for enclosure in item.enclosures],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(item: ParsedItem) -> str:
    return hashlib.sha256(canonical_content(item).encode("utf-8")).hexdigest()


def normalize_item(item: ParsedItem, contract: str = IDENTITY_DEFAULT) -> NormalizedItem:
    identity = identify_item(item, contract)
    return NormalizedItem(
        item_key=identity.item_key,
        raw_id=normalize_text(item.raw_id, collapse=True),
        title=normalize_title(item.title),
        link=normalize_url(item.link),
        summary=normalize_text(item.summary),
        content=normalize_text(item.content),
        published_at=normalize_text(item.published_at),
        authors=tuple(item.authors),
        categories=tuple(item.categories),
        enclosures=tuple(item.enclosures),
        content_hash=content_hash(item),
    )


def changed_fields(existing: Any, item: NormalizedItem) -> list[str]:
    old = existing.comparable_fields()
    new = item.comparable_fields()
    return sorted(key for key in new if old.get(key) != new[key])


def event_detail(item: NormalizedItem, fields: Optional[list[str]] = None) -> Mapping[str, Any]:
    detail: dict[str, Any] = {"content_hash": item.content_hash}
    if fields:
        detail["changed_fields"] = fields
    return detail
