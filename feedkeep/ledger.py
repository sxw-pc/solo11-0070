"""Business orchestration for one deterministic fetch pass."""

from __future__ import annotations

from typing import Iterable, Optional

from .clock import Clock, SystemClock
from .detect import FormatDetectionError, detect_format
from .fetcher import Fetcher
from .identity import IdentityError, normalize_item
from .models import (
    ApplyStats,
    FeedSource,
    FetchResult,
    ItemIssue,
    RunCounts,
    RunSummary,
    SourceRunRecord,
)
from .parsers import ParseError, parse_feed
from .policy import Policy
from .store import Store


class Ledger:
    def __init__(self, store: Store, policy: Policy, fetcher: Optional[Fetcher] = None,
                 clock: Optional[Clock] = None):
        self.store = store
        self.policy = policy
        self.clock = clock or SystemClock()
        self.fetcher = fetcher or Fetcher(policy, self.clock)

    def run(self, sources: Iterable[FeedSource],
            config_path: Optional[str] = None) -> RunSummary:
        configured = list(sources)
        blocked = self.store.sync_sources(configured)
        # Paused/retired sources are skipped exactly like disabled ones: no
        # request, no source_run, no missing reconciliation. Retirement can
        # only be lifted by an explicit resume, never by config sync alone.
        enabled = [
            source for source in configured
            if source.enabled and source.id not in blocked
        ]
        started_at = self.clock.now_iso()
        run_id = self.store.create_run(started_at, config_path)
        records: list[SourceRunRecord] = []
        counts = RunCounts(sources_total=len(enabled))
        for source in enabled:
            record = self._process_source(source, run_id)
            self.store.add_source_run(run_id, record)
            records.append(record)
            counts.sources_ok += int(record.status == "ok")
            counts.sources_not_modified += int(record.status == "not_modified")
            counts.sources_failed += int(record.status == "failed")
            counts.items_seen += record.items_total
            counts.items_new += record.items_new
            counts.items_updated += record.items_updated
            counts.items_missing += record.items_missing
            counts.items_restored += record.items_restored
            counts.item_errors += record.item_errors
        status = self._run_status(counts)
        finished_at = self.clock.now_iso()
        self.store.finish_run(run_id, finished_at, status, counts)
        return RunSummary(
            run_id=run_id, started_at=started_at, finished_at=finished_at,
            status=status, config_path=config_path, counts=counts,
            sources=tuple(sorted(records, key=lambda record: record.source_id)),
        )

    def _run_status(self, counts: RunCounts) -> str:
        if counts.sources_total == 0 or counts.sources_failed == counts.sources_total:
            return "failed"
        return "partial" if counts.sources_failed else "ok"

    def _process_source(self, source: FeedSource, run_id: str) -> SourceRunRecord:
        state = self.store.get_source_state(source.id)
        result = self.fetcher.fetch(
            source.url,
            etag=state.etag if state else None,
            last_modified=state.last_modified if state else None,
        )
        observed_at = self.clock.now_iso()
        if result.not_modified:
            self.store.mark_not_modified(source.id, observed_at, result.etag, result.last_modified)
            return self._record(source, "not_modified", result, observed_at)
        if result.failed:
            self.store.mark_failure(source.id, observed_at)
            return self._record(source, "failed", result, observed_at)
        return self._consume_success(source, run_id, result, observed_at)

    def _consume_success(self, source: FeedSource, run_id: str, result: FetchResult,
                         observed_at: str) -> SourceRunRecord:
        body = result.body or b""
        try:
            fmt = source.format
            if fmt == "auto":
                fmt = detect_format(body, result.content_type)
            parsed = parse_feed(fmt, body)
        except (FormatDetectionError, ParseError) as exc:
            self.store.mark_failure(source.id, observed_at)
            return self._record(
                source, "failed", result, observed_at,
                error_code=exc.code, error_message=exc.message,
            )

        stats = ApplyStats()
        issues = list(parsed.issues)
        normalized = []
        seen: set[str] = set()
        truncated = len(parsed.items) > self.policy.max_items_per_feed
        for index, raw_item in enumerate(parsed.items[: self.policy.max_items_per_feed]):
            try:
                item = normalize_item(raw_item)
            except IdentityError as exc:
                issues.append(ItemIssue(index, exc.code, exc.message))
                continue
            if item.item_key in seen:
                issues.append(ItemIssue(index, "ITEM_DUPLICATE_KEY",
                                        f"条目 {index} 与同一响应中的其他条目身份冲突"))
                continue
            seen.add(item.item_key)
            normalized.append(item)
        try:
            stats = self.store.apply_observation(
                source.id, run_id, observed_at, normalized, issues, truncated=truncated,
            )
        except Exception as exc:
            self.store.mark_failure(source.id, observed_at)
            return self._record(
                source, "failed", result, observed_at,
                error_code="STORE_ERROR", error_message=f"归档失败: {type(exc).__name__}",
            )
        self.store.mark_success(source.id, observed_at, result.etag, result.last_modified)
        error_code = "ITEMS_TRUNCATED" if truncated else None
        error_message = (
            f"仅处理前 {self.policy.max_items_per_feed} 条" if truncated else None
        )
        return self._record(source, "ok", result, observed_at, stats=stats,
                            error_code=error_code, error_message=error_message)

    def _record(self, source: FeedSource, status: str, result: FetchResult,
                observed_at: str, stats: Optional[ApplyStats] = None,
                error_code: Optional[str] = None,
                error_message: Optional[str] = None) -> SourceRunRecord:
        stats = stats or ApplyStats()
        return SourceRunRecord(
            source_id=source.id, source_name=source.name, status=status,
            http_status=result.status, duration_ms=result.duration_ms,
            body_bytes=result.body_bytes, body_sha256=result.body_sha256,
            etag=result.etag, last_modified=result.last_modified,
            attempts=result.attempts, items_total=stats.items_seen,
            items_new=stats.items_new, items_updated=stats.items_updated,
            items_missing=stats.items_missing, items_restored=stats.items_restored,
            item_errors=stats.item_errors,
            error_code=error_code if error_code is not None else result.error_code,
            error_message=error_message if error_message is not None else result.error_message,
            truncated=bool(error_code == "ITEMS_TRUNCATED"),
            observed_at=observed_at,
        )
