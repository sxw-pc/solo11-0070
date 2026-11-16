"""SQLite archive: idempotent observations, lifecycle events and audit queries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .lifecycle import plan_missing, plan_transition
from .models import (ApplyStats, Enclosure, ExistingItem, FeedSource, ItemIssue,
                     NormalizedItem, RunCounts, SourceEventRecord,
                     SourceLifecycleResult, SourceRunRecord, SourceState)
from .schema import initialize


class SourceLifecycleError(Exception):
    """A pause/retire/resume transition was rejected."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        initialize(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # -- source state -------------------------------------------------
    def sync_sources(self, sources: Iterable[FeedSource]) -> set[str]:
        """Upsert the config snapshot and return ids blocked from fetching.

        The ``enabled`` column mirrors the config file; operator lifecycle
        state (``paused_at`` / ``retired_at`` / ``epoch_id``) is never touched
        here, so a retired source cannot be silently resurrected by config.
        """

        with self.conn:
            for source in sources:
                self.conn.execute(
                    """
                    INSERT INTO sources(id,name,url,format,enabled,tags_json,note)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      name=excluded.name,
                      etag=CASE WHEN sources.url<>excluded.url THEN NULL ELSE sources.etag END,
                      last_modified=CASE WHEN sources.url<>excluded.url THEN NULL ELSE sources.last_modified END, url=excluded.url, format=excluded.format,
                      enabled=excluded.enabled, tags_json=excluded.tags_json, note=excluded.note
                    """,
                    (source.id, source.name, source.url, source.format,
                     int(source.enabled), _json(list(source.tags)), source.note),
                )
            rows = self.conn.execute(
                "SELECT id FROM sources WHERE paused_at IS NOT NULL OR retired_at IS NOT NULL"
            ).fetchall()
        return {row["id"] for row in rows}

    def get_source_state(self, source_id: str) -> Optional[SourceState]:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            return None
        return SourceState(
            source_id=row["id"], etag=row["etag"], last_modified=row["last_modified"],
            last_checked_at=row["last_checked_at"], last_success_at=row["last_success_at"],
            consecutive_failures=row["consecutive_failures"],
        )

    def mark_not_modified(self, source_id: str, checked_at: str,
                          etag: Optional[str], last_modified: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE sources SET last_checked_at=?, last_success_at=?,
                   consecutive_failures=0, etag=COALESCE(?, etag),
                   last_modified=COALESCE(?, last_modified) WHERE id=?""",
                (checked_at, checked_at, etag, last_modified, source_id),
            )

    def mark_success(self, source_id: str, checked_at: str, etag: Optional[str],
                     last_modified: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE sources SET last_checked_at=?, last_success_at=?,
                   consecutive_failures=0, etag=?, last_modified=? WHERE id=?""",
                (checked_at, checked_at, etag, last_modified, source_id),
            )

    def mark_failure(self, source_id: str, checked_at: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET last_checked_at=?, consecutive_failures=consecutive_failures+1 "
                "WHERE id=?", (checked_at, source_id))

    # -- source lifecycle (pause / retire / resume) -------------------
    def _source_lifecycle_row(self, source_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT id, enabled, epoch_id, paused_at, retired_at FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise SourceLifecycleError(f"未知来源: {source_id}")
        return row

    def _assert_no_running_source_run(self, source_id: str) -> None:
        row = self.conn.execute(
            """SELECT 1 FROM source_runs sr JOIN runs r ON r.id=sr.run_id
               WHERE sr.source_id=? AND r.status='running' LIMIT 1""",
            (source_id,),
        ).fetchone()
        if row is not None:
            raise SourceLifecycleError(
                f"来源 {source_id} 有正在进行的抓取记录，生命周期转换必须在 run 边界执行"
            )

    def _record_source_event(self, source_id: str, event_type: str, epoch_id: int,
                             occurred_at: str, detail: dict[str, Any]) -> SourceEventRecord:
        # Bind the event to the run boundary it happened after. Lifecycle
        # transitions are rejected while a run is in flight, so the latest
        # finished run is a deterministic logical clock — unlike occurred_at,
        # whose second granularity cannot order a run and a transition that
        # happen within the same second.
        last_run = self.conn.execute(
            "SELECT id FROM runs WHERE status<>'running' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_run_id = last_run["id"] if last_run else None
        self.conn.execute(
            """INSERT INTO source_events(
               source_id,event_type,epoch_id,occurred_at,last_run_id,detail_json)
               VALUES(?,?,?,?,?,?)""",
            (source_id, event_type, epoch_id, occurred_at, last_run_id, _json(detail)),
        )
        return SourceEventRecord(
            source_id=source_id, event_type=event_type, epoch_id=epoch_id,
            occurred_at=occurred_at, last_run_id=last_run_id, detail=detail,
        )

    def _lifecycle_result(self, source_id: str, action: str, changed: bool,
                          event: Optional[SourceEventRecord],
                          message: str) -> SourceLifecycleResult:
        row = self._source_lifecycle_row(source_id)
        blocked = row["paused_at"] is not None or row["retired_at"] is not None
        return SourceLifecycleResult(
            source_id=source_id, action=action, changed=changed,
            enabled=bool(row["enabled"]) and not blocked,
            epoch_id=row["epoch_id"], paused_at=row["paused_at"],
            retired_at=row["retired_at"], event=event, message=message,
        )

    def pause_source(self, source_id: str, paused_at: str) -> SourceLifecycleResult:
        """Stop requesting the source; items and identity history stay active."""

        with self.conn:
            row = self._source_lifecycle_row(source_id)
            self._assert_no_running_source_run(source_id)
            if row["retired_at"] is not None:
                raise SourceLifecycleError(f"来源已退役，不能暂停: {source_id}")
            if row["paused_at"] is not None:
                return self._lifecycle_result(
                    source_id, "pause", False, None, "来源已处于暂停状态")
            self.conn.execute(
                "UPDATE sources SET paused_at=? WHERE id=?", (paused_at, source_id))
            event = self._record_source_event(
                source_id, "paused", row["epoch_id"], paused_at, {})
            return self._lifecycle_result(
                source_id, "pause", True, event,
                "来源已暂停：停止请求，既有条目保持活跃")

    def retire_source(self, source_id: str, retired_at: str) -> SourceLifecycleResult:
        """Close the active archive epoch; items move to the retired inventory.

        One transaction: marks the epoch retired and records a source event.
        Item payloads and item events are preserved untouched; no source_run
        is fabricated and nothing is counted as missing.
        """

        with self.conn:
            row = self._source_lifecycle_row(source_id)
            self._assert_no_running_source_run(source_id)
            if row["retired_at"] is not None:
                return self._lifecycle_result(
                    source_id, "retire", False, None, "来源已退役，重复操作被忽略")
            items_retired = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE source_id=? AND epoch_id=?",
                (source_id, row["epoch_id"]),
            ).fetchone()["n"]
            self.conn.execute(
                "UPDATE sources SET retired_at=? WHERE id=?", (retired_at, source_id))
            event = self._record_source_event(
                source_id, "retired", row["epoch_id"], retired_at,
                {"items_retired": items_retired})
            return self._lifecycle_result(
                source_id, "retire", True, event,
                f"来源已退役：{items_retired} 个条目转入退役清单，历史内容保留")

    def resume_source(self, source_id: str, resumed_at: str,
                      mode: str = "epoch") -> SourceLifecycleResult:
        """Resume a paused/retired source: continue the old epoch or open a new one."""

        if mode not in ("epoch", "new-epoch"):
            raise SourceLifecycleError(f"未知恢复模式: {mode}（可选 epoch / new-epoch）")
        with self.conn:
            row = self._source_lifecycle_row(source_id)
            self._assert_no_running_source_run(source_id)
            if row["retired_at"] is not None:
                if mode == "epoch":
                    self.conn.execute(
                        "UPDATE sources SET retired_at=NULL, paused_at=NULL WHERE id=?",
                        (source_id,))
                    event = self._record_source_event(
                        source_id, "resumed", row["epoch_id"], resumed_at,
                        {"from_epoch": row["epoch_id"]})
                    return self._lifecycle_result(
                        source_id, "resume", True, event,
                        "来源已恢复：沿用退役前的归档纪元，原条目重新进入活跃清单")
                new_epoch = row["epoch_id"] + 1
                # A new epoch re-archives from scratch: drop the conditional
                # request cache so the next fetch observes the full feed.
                self.conn.execute(
                    "UPDATE sources SET retired_at=NULL, paused_at=NULL, epoch_id=?, "
                    "etag=NULL, last_modified=NULL WHERE id=?",
                    (new_epoch, source_id))
                event = self._record_source_event(
                    source_id, "new_epoch", new_epoch, resumed_at,
                    {"previous_epoch": row["epoch_id"]})
                return self._lifecycle_result(
                    source_id, "resume", True, event,
                    f"来源已恢复：开启新归档纪元 {new_epoch}，旧纪元条目保留为历史")
            if mode == "new-epoch":
                raise SourceLifecycleError(
                    f"来源未退役，不能开启新纪元: {source_id}")
            if row["paused_at"] is None:
                return self._lifecycle_result(
                    source_id, "resume", False, None, "来源处于活跃状态，无需恢复")
            self.conn.execute(
                "UPDATE sources SET paused_at=NULL WHERE id=?", (source_id,))
            event = self._record_source_event(
                source_id, "resumed", row["epoch_id"], resumed_at, {})
            return self._lifecycle_result(
                source_id, "resume", True, event, "来源已恢复：继续同一归档纪元")

    # -- runs ---------------------------------------------------------
    def create_run(self, started_at: str, config_path: Optional[str]) -> str:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(CAST(substr(id, 5) AS INTEGER)), 0) AS n FROM runs"
        ).fetchone()
        run_id = f"run-{int(row['n']) + 1:06d}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(id,started_at,status,config_path) VALUES(?,?,?,?)",
                (run_id, started_at, "running", config_path),
            )
        return run_id

    def finish_run(self, run_id: str, finished_at: str, status: str,
                   counts: RunCounts) -> None:
        columns = ", ".join(f"{key}=?" for key in counts.as_dict())
        with self.conn:
            self.conn.execute(
                f"UPDATE runs SET finished_at=?, status=?, {columns} WHERE id=?",
                (finished_at, status, *counts.as_dict().values(), run_id),
            )

    def add_source_run(self, run_id: str, record: SourceRunRecord) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO source_runs(
                   run_id,source_id,source_name,status,http_status,duration_ms,
                   body_bytes,body_sha256,etag,last_modified,attempts,items_total,
                   items_new,items_updated,items_missing,items_restored,item_errors,
                   error_code,error_message,truncated,observed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, record.source_id, record.source_name, record.status,
                 record.http_status, record.duration_ms, record.body_bytes,
                 record.body_sha256, record.etag, record.last_modified, record.attempts,
                 record.items_total, record.items_new, record.items_updated,
                 record.items_missing, record.items_restored, record.item_errors,
                 record.error_code, record.error_message, int(record.truncated),
                 record.observed_at),
            )

    # -- observation / lifecycle -------------------------------------
    def _enclosures(self, source_id: str, epoch_id: int, item_key: str) -> tuple[Enclosure, ...]:
        rows = self.conn.execute(
            """SELECT url,mime_type,declared_length,position FROM item_enclosures
               WHERE source_id=? AND epoch_id=? AND item_key=? ORDER BY position""",
            (source_id, epoch_id, item_key),
        ).fetchall()
        return tuple(Enclosure(r["url"], r["mime_type"], r["declared_length"], r["position"])
                     for r in rows)

    def _existing(self, row: sqlite3.Row) -> ExistingItem:
        return ExistingItem(
            source_id=row["source_id"], item_key=row["item_key"], title=row["title"],
            link=row["link"], summary=row["summary"], content=row["content"],
            published_at=row["published_at"],
            authors=tuple(_json_load(row["authors_json"], [])),
            categories=tuple(_json_load(row["categories_json"], [])),
            enclosures=self._enclosures(row["source_id"], row["epoch_id"], row["item_key"]),
            content_hash=row["content_hash"], missing_since=row["missing_since"],
        )

    def _insert_event(self, source_id: str, epoch_id: int, item_key: str, run_id: str,
                      observed_at: str, event: Any) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO item_events(
               source_id,epoch_id,item_key,run_id,event_type,observed_at,detail_json)
               VALUES(?,?,?,?,?,?,?)""",
            (source_id, epoch_id, item_key, run_id, event.event_type, observed_at,
             _json(dict(event.detail))),
        )

    def _replace_enclosures(self, source_id: str, epoch_id: int, item_key: str,
                            item: NormalizedItem) -> None:
        self.conn.execute(
            "DELETE FROM item_enclosures WHERE source_id=? AND epoch_id=? AND item_key=?",
            (source_id, epoch_id, item_key),
        )
        self.conn.executemany(
            """INSERT INTO item_enclosures(
               source_id,epoch_id,item_key,position,url,mime_type,declared_length)
               VALUES(?,?,?,?,?,?,?)""",
            [(source_id, epoch_id, item_key, enc.position, enc.url, enc.mime_type,
              enc.declared_length) for enc in item.enclosures],
        )

    def _current_epoch(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT epoch_id, retired_at FROM sources WHERE id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise SourceLifecycleError(f"未知来源: {source_id}")
        if row["retired_at"] is not None:
            raise SourceLifecycleError(
                f"来源已退役，不再接受观察: {source_id}（如需继续请先 resume）")
        return int(row["epoch_id"])

    def apply_observation(self, source_id: str, run_id: str, observed_at: str,
                          items: Sequence[NormalizedItem], issues: Sequence[ItemIssue],
                          truncated: bool = False) -> ApplyStats:
        stats = ApplyStats()
        seen: set[str] = set()
        with self.conn:
            epoch_id = self._current_epoch(source_id)
            rows = {
                row["item_key"]: row
                for row in self.conn.execute(
                    "SELECT * FROM items WHERE source_id=? AND epoch_id=?",
                    (source_id, epoch_id),
                ).fetchall()
            }
            for item in items:
                if item.item_key in seen:
                    continue
                seen.add(item.item_key)
                row = rows.get(item.item_key)
                existing = self._existing(row) if row is not None else None
                transition = plan_transition(existing, item)
                values = (item.raw_id, item.title, item.link, item.summary, item.content,
                          item.published_at, _json(list(item.authors)),
                          _json(list(item.categories)), item.content_hash)
                if existing is None:
                    self.conn.execute(
                        """INSERT INTO items(
                           source_id,epoch_id,item_key,raw_id,title,link,summary,content,
                           published_at,authors_json,categories_json,content_hash,
                           first_seen_at,last_seen_at,last_changed_at,missing_since,
                           first_run_id,last_run_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                        (source_id, epoch_id, item.item_key, *values, observed_at, observed_at,
                         observed_at, run_id, run_id),
                    )
                    stats.items_new += 1
                else:
                    last_changed = observed_at if transition.is_updated else row["last_changed_at"]
                    self.conn.execute(
                        """UPDATE items SET raw_id=?,title=?,link=?,summary=?,content=?,
                           published_at=?,authors_json=?,categories_json=?,content_hash=?,
                           last_seen_at=?,last_changed_at=?,missing_since=NULL,last_run_id=?
                           WHERE source_id=? AND epoch_id=? AND item_key=?""",
                        (*values, observed_at, last_changed, run_id,
                         source_id, epoch_id, item.item_key),
                    )
                    stats.items_updated += int(transition.is_updated)
                    stats.items_restored += int(transition.is_restored)
                self._replace_enclosures(source_id, epoch_id, item.item_key, item)
                for event in transition.events:
                    self._insert_event(source_id, epoch_id, item.item_key, run_id,
                                       observed_at, event)
                stats.items_seen += 1

            if not truncated:
                for key, row in sorted(rows.items()):
                    if key in seen or row["missing_since"] is not None:
                        continue
                    self.conn.execute(
                        """UPDATE items SET missing_since=?
                           WHERE source_id=? AND epoch_id=? AND item_key=?""",
                        (observed_at, source_id, epoch_id, key),
                    )
                    existing = self._existing(row)
                    self._insert_event(source_id, epoch_id, key, run_id, observed_at,
                                       plan_missing(existing))
                    stats.items_missing += 1

            for issue in issues:
                self.conn.execute(
                    """INSERT INTO item_issues(run_id,source_id,item_index,code,message)
                       VALUES(?,?,?,?,?)""",
                    (run_id, source_id, issue.index, issue.code, issue.message),
                )
            stats.item_errors = len(issues)
        return stats

    # -- report queries ----------------------------------------------
    def latest_run_id(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT id FROM runs WHERE status<>'running' ORDER BY rowid DESC LIMIT 1").fetchone()
        return row["id"] if row else None

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_source_runs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM source_runs WHERE run_id=? ORDER BY source_id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_item_issues(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT source_id,item_index,code,message FROM item_issues
               WHERE run_id=? ORDER BY source_id,item_index,id""", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT source_id,epoch_id,item_key,event_type,observed_at,detail_json
               FROM item_events WHERE run_id=? ORDER BY source_id,item_key,id""",
            (run_id,),
        ).fetchall()
        return [{
            "source_id": row["source_id"], "epoch_id": row["epoch_id"],
            "item_key": row["item_key"],
            "event_type": row["event_type"], "observed_at": row["observed_at"],
            "detail": _json_load(row["detail_json"], {}),
        } for row in rows]

    def get_source_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT source_id,event_type,epoch_id,occurred_at,last_run_id,detail_json
               FROM source_events ORDER BY occurred_at,id"""
        ).fetchall()
        return [{
            "source_id": row["source_id"], "event_type": row["event_type"],
            "epoch_id": row["epoch_id"], "occurred_at": row["occurred_at"],
            "last_run_id": row["last_run_id"],
            "detail": _json_load(row["detail_json"], {}),
        } for row in rows]

    def inventory_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Per-source inventory as of the moment the run finished.

        Buckets are reconstructed at the run boundary: an item counts if its
        first-seen run is not later than this run; missing state replays
        missing/restored events up to this run; lifecycle state replays
        source events bound to an earlier run boundary. Items of a retired
        epoch are reported as ``retired`` regardless of their missing flag.
        """

        run = self.conn.execute(
            "SELECT rowid FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            return []
        run_rowid = run["rowid"]

        default_state = {"epoch_id": 1, "paused": False, "retired": False}
        lifecycle: dict[str, dict[str, Any]] = {}
        for event in self.conn.execute(
            """SELECT se.source_id, se.event_type, se.epoch_id
               FROM source_events se LEFT JOIN runs lr ON lr.id=se.last_run_id
               WHERE se.last_run_id IS NULL OR lr.rowid<?
               ORDER BY se.id""",
            (run_rowid,),
        ).fetchall():
            state = lifecycle.setdefault(event["source_id"], dict(default_state))
            if event["event_type"] == "new_epoch":
                state.update(epoch_id=event["epoch_id"], retired=False, paused=False)
            elif event["event_type"] == "retired":
                state["retired"] = True
            elif event["event_type"] == "resumed":
                state.update(retired=False, paused=False)
            elif event["event_type"] == "paused":
                state["paused"] = True

        buckets: dict[str, dict[str, int]] = {}

        def bucket(source_id: str) -> dict[str, int]:
            return buckets.setdefault(
                source_id, {"total": 0, "active": 0, "missing": 0, "retired": 0})

        item_rows = self.conn.execute(
            """SELECT i.source_id, i.epoch_id,
                      (SELECT e.event_type FROM item_events e
                       JOIN runs er ON er.id=e.run_id
                       WHERE e.source_id=i.source_id AND e.epoch_id=i.epoch_id
                         AND e.item_key=i.item_key
                         AND e.event_type IN ('missing','restored')
                         AND er.rowid<=?
                       ORDER BY e.id DESC LIMIT 1) AS flip
               FROM items i JOIN runs fr ON fr.id=i.first_run_id
               WHERE fr.rowid<=?""",
            (run_rowid, run_rowid),
        ).fetchall()
        for item in item_rows:
            state = lifecycle.get(item["source_id"], default_state)
            is_retired = item["epoch_id"] != state["epoch_id"] or state["retired"]
            counts = bucket(item["source_id"])
            counts["total"] += 1
            if is_retired:
                counts["retired"] += 1
            elif item["flip"] == "missing":
                counts["missing"] += 1
            else:
                counts["active"] += 1

        for row in self.conn.execute(
                "SELECT source_id FROM source_runs WHERE run_id=?", (run_id,)).fetchall():
            bucket(row["source_id"])
        for row in self.conn.execute(
                """SELECT DISTINCT se.source_id FROM source_events se
                   LEFT JOIN runs lr ON lr.id=se.last_run_id
                   WHERE se.last_run_id IS NULL OR lr.rowid<?""",
                (run_rowid,)).fetchall():
            bucket(row["source_id"])

        inventory = []
        for source_id in sorted(buckets):
            state = lifecycle.get(source_id, default_state)
            if state["retired"]:
                status = "retired"
            elif state["paused"]:
                status = "paused"
            else:
                status = "active"
            counts = buckets[source_id]
            inventory.append({
                "source_id": source_id,
                "epoch_id": state["epoch_id"],
                "lifecycle": status,
                "total": counts["total"],
                "active": counts["active"],
                "missing": counts["missing"],
                "retired": counts["retired"],
            })
        return inventory
