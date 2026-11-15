"""Business orchestration for one deterministic fetch pass.

A run records one source at a time and only writes its final summary after the
last source.  A process that dies in between leaves a ``running`` run with a
prefix of ``source_runs``.  Recovery is explicit and offers two complete,
business-distinct routes:

* ``resume`` continues the *same* round, but only after the persisted plan
  (configured source order plus policy summary) matches the current config and
  every recorded outcome passes its integrity digest;
* ``finalize`` freezes the old round as ``interrupted`` and executes a fresh
  round, keeping the partial facts and the complete facts in separate audit
  records.
"""

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
    RecoveryOutcome,
    RunCounts,
    RunSummary,
    SourceRunRecord,
)
from .parsers import ParseError, parse_feed
from .policy import Policy
from .recovery import (
    RecoveryError,
    build_run_snapshot,
    enabled_source_ids,
    run_fingerprint,
)
from .store import Store


class Ledger:
    def __init__(self, store: Store, policy: Policy, fetcher: Optional[Fetcher] = None,
                 clock: Optional[Clock] = None):
        self.store = store
        self.policy = policy
        self.clock = clock or SystemClock()
        self.fetcher = fetcher or Fetcher(policy, self.clock)

    # -- entry points -------------------------------------------------
    def run(self, sources: Iterable[FeedSource],
            config_path: Optional[str] = None) -> RunSummary:
        configured = list(sources)
        snapshot = build_run_snapshot(configured, self.policy)
        return self._execute(
            configured, config_path, snapshot, run_fingerprint(snapshot),
        )

    def resume(self, run_id: str, sources: Iterable[FeedSource],
               config_path: Optional[str] = None) -> RecoveryOutcome:
        """Continue an interrupted run in place without refetching old sources."""

        configured = list(sources)
        snapshot = build_run_snapshot(configured, self.policy)
        fingerprint = run_fingerprint(snapshot)

        row = self.store.get_run(run_id)
        if row is None:
            raise RecoveryError("RECOVERY_RUN_NOT_FOUND", f"找不到抓取记录: {run_id}")
        if row.get("status") != "running":
            raise RecoveryError(
                "RECOVERY_NOT_RUNNING",
                f"抓取记录 {run_id} 当前状态为 {row.get('status')!r}，只有 running 轮次可以继续",
            )
        stored_fingerprint = row.get("fingerprint")
        if not stored_fingerprint:
            raise RecoveryError(
                "RECOVERY_PLAN_MISSING",
                f"抓取记录 {run_id} 没有持久化来源与策略快照，无法验证是否为同一轮",
            )
        if stored_fingerprint != fingerprint:
            raise RecoveryError(
                "RECOVERY_PLAN_MISMATCH",
                f"当前配置与中断轮次 {run_id} 的来源集合、顺序或策略摘要不一致，"
                "拒绝在同一轮内混用两份配置",
            )

        stored_snapshot = self.store.get_run_snapshot(run_id)
        expected_ids = enabled_source_ids(stored_snapshot)
        recorded = self.store.verify_recorded_source_runs(run_id)
        recorded_map = {record.source_id: record for record in recorded}
        if len(recorded_map) != len(recorded):
            raise RecoveryError(
                "RECOVERY_SOURCE_STATE_INVALID",
                f"抓取记录 {run_id} 存在重复来源记录，无法确认未完成位置",
            )
        recorded_ids = [source_id for source_id in expected_ids if source_id in recorded_map]
        if (set(recorded_ids) != set(recorded_map)
                or recorded_ids != expected_ids[: len(recorded_ids)]):
            raise RecoveryError(
                "RECOVERY_SOURCE_STATE_INVALID",
                f"抓取记录 {run_id} 的已完成来源 {sorted(recorded_map)!r} "
                "不是配置顺序的连续前缀，无法确认未完成位置",
            )
        by_id = {source.id: source for source in configured}
        pending_ids = expected_ids[len(recorded_ids):]
        pending = [by_id[source_id] for source_id in pending_ids]
        if any(not by_id[source_id].enabled for source_id in pending_ids):
            raise RecoveryError(
                "RECOVERY_PLAN_MISMATCH",
                f"抓取记录 {run_id} 的待处理来源已不再启用",
            )

        self.store.sync_sources(configured)
        resumed_at = self.clock.now_iso()
        self.store.begin_resume(run_id, resumed_at)
        self.store.set_recovery_lists(
            run_id, reused_source_ids=recorded_ids, processed_source_ids=[],
        )

        records = self._process_remaining(pending, run_id)
        processed_ids = [record.source_id for record in records]
        all_records = self.store.get_source_run_records(run_id)
        counts = RunCounts.from_source_runs(all_records, sources_total=len(expected_ids))
        status = self._run_status(counts)
        finished_at = self.clock.now_iso()
        self.store.finish_run(run_id, finished_at, status, counts)
        self.store.set_recovery_lists(
            run_id, reused_source_ids=recorded_ids,
            processed_source_ids=processed_ids,
        )
        summary = RunSummary(
            run_id=run_id, started_at=row["started_at"], finished_at=finished_at,
            status=status, config_path=row.get("config_path"), counts=counts,
            sources=tuple(sorted(all_records, key=lambda record: record.source_id)),
        )
        return RecoveryOutcome(
            action="resume", run_id=run_id, interrupted_run_id=None,
            plan_changed=False, reused_source_ids=tuple(recorded_ids),
            processed_source_ids=tuple(processed_ids), summary=summary,
        )

    def finalize(self, run_id: str, sources: Iterable[FeedSource],
                 config_path: Optional[str] = None) -> RecoveryOutcome:
        """Freeze an interrupted run and execute a full replacement round."""

        configured = list(sources)
        row = self.store.get_run(run_id)
        if row is None:
            raise RecoveryError("RECOVERY_RUN_NOT_FOUND", f"找不到抓取记录: {run_id}")
        if row.get("status") != "running":
            raise RecoveryError(
                "RECOVERY_NOT_RUNNING",
                f"抓取记录 {run_id} 当前状态为 {row.get('status')!r}，只有 running 轮次可以定格",
            )

        recorded = self.store.get_source_run_records(run_id)
        old_snapshot = self.store.get_run_snapshot(run_id)
        old_total = (
            len(enabled_source_ids(old_snapshot))
            if old_snapshot is not None
            else len(recorded)
        )
        old_counts = RunCounts.from_source_runs(recorded, sources_total=old_total)

        snapshot = build_run_snapshot(configured, self.policy)
        fingerprint = run_fingerprint(snapshot)
        plan_changed = old_snapshot is None or row.get("fingerprint") != fingerprint
        interrupted_at = self.clock.now_iso()
        new_run_id = self.store.freeze_and_create(
            run_id, interrupted_at, counts=old_counts, config_path=config_path,
            snapshot=snapshot, fingerprint=fingerprint,
        )

        summary = self._execute(
            configured, config_path, snapshot, fingerprint,
            run_id=new_run_id, started_at=interrupted_at,
        )
        return RecoveryOutcome(
            action="finalize", run_id=new_run_id, interrupted_run_id=run_id,
            plan_changed=plan_changed,
            reused_source_ids=(), processed_source_ids=tuple(
                record.source_id for record in summary.sources
            ), summary=summary,
        )

    # -- shared execution --------------------------------------------
    def _execute(self, configured: list[FeedSource], config_path: Optional[str],
                 snapshot: dict, fingerprint: str, run_id: Optional[str] = None,
                 started_at: Optional[str] = None) -> RunSummary:
        enabled = [source for source in configured if source.enabled]
        self.store.sync_sources(configured)
        started_at = started_at or self.clock.now_iso()
        if run_id is None:
            run_id = self.store.create_run(
                started_at, config_path, snapshot=snapshot, fingerprint=fingerprint,
            )
        records = self._process_remaining(enabled, run_id)
        counts = RunCounts.from_source_runs(records, sources_total=len(enabled))
        status = self._run_status(counts)
        finished_at = self.clock.now_iso()
        self.store.finish_run(run_id, finished_at, status, counts)
        self.store.set_recovery_lists(
            run_id, reused_source_ids=self.store.get_recovery_sources(run_id, "reused"),
            processed_source_ids=[record.source_id for record in records],
        )
        return RunSummary(
            run_id=run_id, started_at=started_at, finished_at=finished_at,
            status=status, config_path=config_path, counts=counts,
            sources=tuple(sorted(records, key=lambda record: record.source_id)),
        )

    def _process_remaining(self, pending: Iterable[FeedSource],
                           run_id: str) -> list[SourceRunRecord]:
        records: list[SourceRunRecord] = []
        for source in pending:
            record = self._process_source(source, run_id)
            self.store.add_source_run(run_id, record)
            records.append(record)
        return records

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
