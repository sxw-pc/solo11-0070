"""Persisted run plans and integrity helpers for interrupted-run recovery.

A run plan is the exact processing contract of one fetch pass: the configured
sources in file order plus the policy summary that bounded the requests.  The
plan digest lets an explicit resume prove that the current config is the same
one the interrupted run started with.  Source-run digests provide the second
half of that proof: every already-recorded source outcome is checked against
the bytes that were written, so a hand-edited or half-written row cannot be
silently carried into a resumed round.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

from .models import FeedSource, SourceRunRecord
from .policy import Policy

SNAPSHOT_VERSION = 1
SOURCE_RECORD_FIELDS = (
    "source_id", "source_name", "status", "http_status", "duration_ms",
    "body_bytes", "body_sha256", "etag", "last_modified", "attempts",
    "items_total", "items_new", "items_updated", "items_missing",
    "items_restored", "item_errors", "error_code", "error_message",
    "truncated", "observed_at",
)


class RecoveryError(Exception):
    """An explicit recovery request that cannot be honoured safely."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def policy_summary(policy: Policy) -> dict[str, Any]:
    """The policy fields that change what a source outcome means."""

    return {
        "allowed_hosts": sorted(policy.allowed_hosts),
        "timeout_seconds": policy.timeout_seconds,
        "max_body_bytes": policy.max_body_bytes,
        "max_items_per_feed": policy.max_items_per_feed,
        "min_interval_ms": policy.min_interval_ms,
        "max_retries": policy.max_retries,
        "backoff_base_ms": policy.backoff_base_ms,
    }


def build_run_snapshot(sources: Iterable[FeedSource], policy: Policy) -> dict[str, Any]:
    """Freeze source order and policy into a comparable, persisted document."""

    return {
        "version": SNAPSHOT_VERSION,
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "url": source.url,
                "format": source.format,
                "enabled": bool(source.enabled),
            }
            for source in sources
        ],
        "policy": policy_summary(policy),
    }


def _plan_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a snapshot that determines whether two rounds are equal.

    Disabled entries never take part in a round, so they are excluded: adding
    or removing one does not change what the interrupted round was doing.
    """

    return {
        "version": snapshot.get("version"),
        "sources": [
            {
                "id": source["id"],
                "url": source.get("url"),
                "format": source.get("format"),
            }
            for source in snapshot.get("sources", ())
            if source.get("enabled")
        ],
        "policy": snapshot.get("policy"),
    }


def run_fingerprint(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(_plan_projection(snapshot)).encode("utf-8")
    ).hexdigest()


def enabled_source_ids(snapshot: Optional[Mapping[str, Any]]) -> list[str]:
    """Source ids in the order a run processed them, or [] without a snapshot."""

    if not snapshot:
        return []
    return [
        str(source["id"])
        for source in snapshot.get("sources", ())
        if source.get("enabled")
    ]


def source_record_payload(run_id: str, record: SourceRunRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(record, SourceRunRecord):
        row = record.as_dict()
    else:
        row = {field: record.get(field) for field in SOURCE_RECORD_FIELDS}
    row = {field: row.get(field) for field in SOURCE_RECORD_FIELDS}
    row["truncated"] = bool(row["truncated"])
    row["run_id"] = run_id
    return row


def source_record_digest(run_id: str, record: SourceRunRecord | Mapping[str, Any]) -> str:
    payload = source_record_payload(run_id, record)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_source_record(run_id: str, row: Mapping[str, Any]) -> None:
    """Reject a persisted source outcome that cannot be verified as intact."""

    source_id = row.get("source_id")
    digest = row.get("record_digest")
    if not digest:
        raise RecoveryError(
            "RECOVERY_SOURCE_STATE_INVALID",
            f"来源 {source_id} 的已完成状态缺少完整性摘要，无法安全续跑",
        )
    if digest != source_record_digest(run_id, row):
        raise RecoveryError(
            "RECOVERY_SOURCE_STATE_INVALID",
            f"来源 {source_id} 的已完成状态与摘要不一致，无法安全续跑",
        )
    if row.get("status") not in {"ok", "not_modified", "failed"}:
        raise RecoveryError(
            "RECOVERY_SOURCE_STATE_INVALID",
            f"来源 {source_id} 的状态 {row.get('status')!r} 非法",
        )
    if not row.get("observed_at"):
        raise RecoveryError(
            "RECOVERY_SOURCE_STATE_INVALID",
            f"来源 {source_id} 的已完成状态缺少观察时间，无法安全续跑",
        )
