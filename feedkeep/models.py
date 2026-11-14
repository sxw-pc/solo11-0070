"""Typed domain objects shared by the collector, archive and reporter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class FeedSource:
    """A configured subscription source."""

    id: str
    name: str
    url: str
    format: str
    enabled: bool
    tags: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class Enclosure:
    """Metadata for an enclosed resource. Binary content is never fetched."""

    url: str
    mime_type: Optional[str] = None
    declared_length: Optional[int] = None
    position: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "mime_type": self.mime_type,
            "declared_length": self.declared_length,
            "position": self.position,
        }


@dataclass(frozen=True)
class ParsedItem:
    """Format-neutral item emitted by a parser before identity is assigned."""

    raw_id: Optional[str]
    title: Optional[str]
    link: Optional[str]
    summary: Optional[str]
    content: Optional[str]
    published_at: Optional[str]
    authors: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    enclosures: tuple[Enclosure, ...] = ()


@dataclass(frozen=True)
class ItemIssue:
    """A recoverable, item-scoped parsing problem."""

    index: int
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ParsedFeed:
    format: str
    items: tuple[ParsedItem, ...]
    issues: tuple[ItemIssue, ...] = ()


@dataclass(frozen=True)
class NormalizedItem:
    """An item with stable identity and a content fingerprint."""

    item_key: str
    raw_id: Optional[str]
    title: Optional[str]
    link: Optional[str]
    summary: Optional[str]
    content: Optional[str]
    published_at: Optional[str]
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    enclosures: tuple[Enclosure, ...]
    content_hash: str

    def comparable_fields(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "content": self.content,
            "published_at": self.published_at,
            "authors": list(self.authors),
            "categories": list(self.categories),
            "enclosures": [e.as_dict() for e in self.enclosures],
        }


@dataclass(frozen=True)
class ExistingItem:
    """The persisted projection needed to plan a lifecycle transition."""

    source_id: str
    item_key: str
    title: Optional[str]
    link: Optional[str]
    summary: Optional[str]
    content: Optional[str]
    published_at: Optional[str]
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    enclosures: tuple[Enclosure, ...]
    content_hash: str
    missing_since: Optional[str]

    def comparable_fields(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "content": self.content,
            "published_at": self.published_at,
            "authors": list(self.authors),
            "categories": list(self.categories),
            "enclosures": [e.as_dict() for e in self.enclosures],
        }


@dataclass(frozen=True)
class ItemEvent:
    event_type: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceState:
    source_id: str
    etag: Optional[str]
    last_modified: Optional[str]
    last_checked_at: Optional[str]
    last_success_at: Optional[str]
    consecutive_failures: int


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one logical fetch (including retries)."""

    url: str
    status: Optional[int]
    attempts: int
    duration_ms: int
    body_bytes: int
    body_sha256: Optional[str]
    body: Optional[bytes] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_type: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error_code is None and self.status == 200

    @property
    def not_modified(self) -> bool:
        return self.error_code is None and self.status == 304

    @property
    def failed(self) -> bool:
        return not self.ok and not self.not_modified


@dataclass
class ApplyStats:
    items_seen: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_missing: int = 0
    items_restored: int = 0
    item_errors: int = 0


@dataclass
class RunCounts:
    sources_total: int = 0
    sources_ok: int = 0
    sources_not_modified: int = 0
    sources_failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_missing: int = 0
    items_restored: int = 0
    item_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sources_total": self.sources_total,
            "sources_ok": self.sources_ok,
            "sources_not_modified": self.sources_not_modified,
            "sources_failed": self.sources_failed,
            "items_seen": self.items_seen,
            "items_new": self.items_new,
            "items_updated": self.items_updated,
            "items_missing": self.items_missing,
            "items_restored": self.items_restored,
            "item_errors": self.item_errors,
        }


@dataclass(frozen=True)
class SourceRunRecord:
    source_id: str
    source_name: str
    status: str
    http_status: Optional[int]
    duration_ms: int
    body_bytes: int
    body_sha256: Optional[str]
    etag: Optional[str]
    last_modified: Optional[str]
    attempts: int
    items_total: int
    items_new: int
    items_updated: int
    items_missing: int
    items_restored: int
    item_errors: int
    error_code: Optional[str]
    error_message: Optional[str]
    truncated: bool
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "status": self.status,
            "http_status": self.http_status,
            "duration_ms": self.duration_ms,
            "body_bytes": self.body_bytes,
            "body_sha256": self.body_sha256,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "attempts": self.attempts,
            "items_total": self.items_total,
            "items_new": self.items_new,
            "items_updated": self.items_updated,
            "items_missing": self.items_missing,
            "items_restored": self.items_restored,
            "item_errors": self.item_errors,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "truncated": self.truncated,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    started_at: str
    finished_at: str
    status: str
    config_path: Optional[str]
    counts: RunCounts
    sources: tuple[SourceRunRecord, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "config_path": self.config_path,
        }
        payload.update(self.counts.as_dict())
        payload["sources"] = [s.as_dict() for s in self.sources]
        return payload

