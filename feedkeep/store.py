"""SQLite archive: idempotent observations, lifecycle events and audit queries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .lifecycle import plan_missing, plan_retraction, plan_transition
from .models import (RETENTION_ERASE, RETENTION_RETAIN, ApplyStats, Enclosure,
                     ExistingItem, FeedSource, ItemIssue, NormalizedItem,
                     NormalizedTombstone, RunCounts, SourceRunRecord, SourceState)
from .schema import initialize


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
    def sync_sources(self, sources: Iterable[FeedSource]) -> None:
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
                   items_new,items_updated,items_missing,items_restored,
                   items_retracted,item_errors,
                   error_code,error_message,truncated,observed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, record.source_id, record.source_name, record.status,
                 record.http_status, record.duration_ms, record.body_bytes,
                 record.body_sha256, record.etag, record.last_modified, record.attempts,
                 record.items_total, record.items_new, record.items_updated,
                 record.items_missing, record.items_restored,
                 record.items_retracted, record.item_errors,
                 record.error_code, record.error_message, int(record.truncated),
                 record.observed_at),
            )

    # -- observation / lifecycle -------------------------------------
    def _enclosures(self, source_id: str, item_key: str) -> tuple[Enclosure, ...]:
        rows = self.conn.execute(
            """SELECT url,mime_type,declared_length,position FROM item_enclosures
               WHERE source_id=? AND item_key=? ORDER BY position""",
            (source_id, item_key),
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
            enclosures=self._enclosures(row["source_id"], row["item_key"]),
            content_hash=row["content_hash"], missing_since=row["missing_since"],
            last_changed_at=row["last_changed_at"],
            retracted_at=row["retracted_at"], updated_at=row["updated_at"],
        )

    def _insert_event(self, source_id: str, item_key: str, run_id: str,
                      observed_at: str, event: Any) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO item_events(
               source_id,item_key,run_id,event_type,observed_at,detail_json)
               VALUES(?,?,?,?,?,?)""",
            (source_id, item_key, run_id, event.event_type, observed_at, _json(dict(event.detail))),
        )

    def _replace_enclosures(self, source_id: str, item_key: str,
                            item: NormalizedItem) -> None:
        self.conn.execute(
            "DELETE FROM item_enclosures WHERE source_id=? AND item_key=?",
            (source_id, item_key),
        )
        self.conn.executemany(
            """INSERT INTO item_enclosures(
               source_id,item_key,position,url,mime_type,declared_length)
               VALUES(?,?,?,?,?,?)""",
            [(source_id, item_key, enc.position, enc.url, enc.mime_type, enc.declared_length)
             for enc in item.enclosures],
        )

    def _insert_issue(self, run_id: str, source_id: str, issue: ItemIssue) -> None:
        self.conn.execute(
            """INSERT INTO item_issues(run_id,source_id,item_index,code,message)
               VALUES(?,?,?,?,?)""",
            (run_id, source_id, issue.index, issue.code, issue.message),
        )

    def _apply_retraction(self, source_id: str, item_key: str, run_id: str,
                          observed_at: str, tombstone: NormalizedTombstone,
                          retention_mode: str) -> None:
        """Move one item into the retracted state inside the open transaction."""

        if retention_mode == RETENTION_ERASE:
            # Keep identity, fingerprints and audit evidence, but remove the
            # retracted publisher content from the archive.
            self.conn.execute(
                """UPDATE items SET title=NULL, link=NULL, summary=NULL, content=NULL,
                   published_at=NULL, authors_json=?, categories_json=?,
                   missing_since=NULL, retracted_at=?, last_run_id=?
                   WHERE source_id=? AND item_key=?""",
                ("[]", "[]", tombstone.declared_at, run_id, source_id, item_key),
            )
            self.conn.execute(
                "DELETE FROM item_enclosures WHERE source_id=? AND item_key=?",
                (source_id, item_key),
            )
        else:
            # Retain the original body, enclosures and content_hash for
            # internal historical queries; only the inventory state changes.
            self.conn.execute(
                """UPDATE items SET missing_since=NULL, retracted_at=?, last_run_id=?
                   WHERE source_id=? AND item_key=?""",
                (tombstone.declared_at, run_id, source_id, item_key),
            )

    def apply_observation(self, source_id: str, run_id: str, observed_at: str,
                          items: Sequence[NormalizedItem], issues: Sequence[ItemIssue],
                          truncated: bool = False,
                          tombstones: Sequence[NormalizedTombstone] = (),
                          retention_mode: str = RETENTION_RETAIN) -> ApplyStats:
        stats = ApplyStats()
        seen: set[str] = set()
        retracted_keys: set[str] = set()
        stale_issues: list[ItemIssue] = []
        with self.conn:
            rows = {
                row["item_key"]: row
                for row in self.conn.execute(
                    "SELECT * FROM items WHERE source_id=?", (source_id,)
                ).fetchall()
            }
            markers = {marker.item_key: marker for marker in tombstones}
            for item in items:
                if item.item_key in seen:
                    continue
                seen.add(item.item_key)
                row = rows.get(item.item_key)
                existing = self._existing(row) if row is not None else None
                marker = markers.pop(item.item_key, None)
                if marker is not None:
                    # Same response carries both an entry and a tombstone for
                    # the same ref: the winner is decided by event time, never
                    # by XML order.
                    decision = plan_retraction(
                        existing, marker, live_update=item.updated_at,
                    )
                    if decision.accepted:
                        self._apply_retraction(
                            source_id, item.item_key, run_id, observed_at,
                            marker, retention_mode,
                        )
                        retracted_keys.add(item.item_key)
                        if decision.event is not None:
                            self._insert_event(
                                source_id, item.item_key, run_id,
                                observed_at, decision.event,
                            )
                        stats.items_retracted += 1
                        continue
                    if decision.stale or existing is None:
                        if not decision.stale:
                            # Nothing archived to withdraw: keep the live entry
                            # and flag the unbacked marker for auditability.
                            stale_issues.append(ItemIssue(
                                index=marker.index, code="TOMBSTONE_STALE",
                                message=(
                                    f"删除标记 {marker.index} 没有可撤回的已归档条目，"
                                    "保留同响应中的新条目"
                                ),
                            ))
                        else:
                            stale_issues.append(ItemIssue(
                                index=marker.index, code="TOMBSTONE_STALE",
                                message=(
                                    f"删除标记 {marker.index} 声明时间 {marker.declared_at} "
                                    "早于条目最新内容，已拒绝撤回"
                                ),
                            ))
                transition = plan_transition(existing, item)
                values = (item.raw_id, item.title, item.link, item.summary, item.content,
                          item.published_at, _json(list(item.authors)),
                          _json(list(item.categories)), item.content_hash)
                if existing is None:
                    self.conn.execute(
                        """INSERT INTO items(
                           source_id,item_key,raw_id,title,link,summary,content,published_at,
                           authors_json,categories_json,content_hash,first_seen_at,last_seen_at,
                           last_changed_at,missing_since,retracted_at,updated_at,
                           first_run_id,last_run_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)""",
                        (source_id, item.item_key, *values, observed_at, observed_at,
                         observed_at, item.updated_at, run_id, run_id),
                    )
                    stats.items_new += 1
                else:
                    last_changed = observed_at if transition.is_updated else row["last_changed_at"]
                    self.conn.execute(
                        """UPDATE items SET raw_id=?,title=?,link=?,summary=?,content=?,
                           published_at=?,authors_json=?,categories_json=?,content_hash=?,
                           last_seen_at=?,last_changed_at=?,missing_since=NULL,
                           retracted_at=NULL,updated_at=?,last_run_id=?
                           WHERE source_id=? AND item_key=?""",
                        (*values, observed_at, last_changed, item.updated_at, run_id,
                         source_id, item.item_key),
                    )
                    stats.items_updated += int(transition.is_updated)
                    stats.items_restored += int(transition.is_restored)
                self._replace_enclosures(source_id, item.item_key, item)
                for event in transition.events:
                    self._insert_event(source_id, item.item_key, run_id, observed_at, event)
                stats.items_seen += 1

            # Tombstones whose ref did not also appear as a live entry. These
            # are processed even on truncated responses: a parsed deletion
            # marker is explicit evidence, not an absence inference.
            for key in sorted(markers):
                marker = markers[key]
                row = rows.get(key)
                existing = self._existing(row) if row is not None else None
                decision = plan_retraction(existing, marker)
                if decision.stale:
                    stale_issues.append(ItemIssue(
                        index=marker.index, code="TOMBSTONE_STALE",
                        message=(
                            f"删除标记 {marker.index} 声明时间 {marker.declared_at} "
                            "早于条目最新内容，已拒绝撤回"
                        ),
                    ))
                    continue
                if not decision.accepted:
                    # Unknown ref (nothing archived) or already retracted:
                    # no state change and no terminal event.
                    continue
                self._apply_retraction(
                    source_id, key, run_id, observed_at, marker, retention_mode,
                )
                retracted_keys.add(key)
                if decision.event is not None:
                    self._insert_event(
                        source_id, key, run_id, observed_at, decision.event,
                    )
                stats.items_retracted += 1

            if not truncated:
                for key, row in sorted(rows.items()):
                    if key in seen or key in retracted_keys:
                        continue
                    if row["missing_since"] is not None:
                        continue
                    if row["retracted_at"] is not None:
                        continue
                    self.conn.execute(
                        "UPDATE items SET missing_since=? WHERE source_id=? AND item_key=?",
                        (observed_at, source_id, key),
                    )
                    existing = self._existing(row)
                    self._insert_event(source_id, key, run_id, observed_at, plan_missing(existing))
                    stats.items_missing += 1

            for issue in list(issues) + stale_issues:
                self._insert_issue(run_id, source_id, issue)
            stats.item_errors = len(issues) + len(stale_issues)
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
            """SELECT source_id,item_key,event_type,observed_at,detail_json
               FROM item_events WHERE run_id=? ORDER BY source_id,item_key,id""",
            (run_id,),
        ).fetchall()
        return [{
            "source_id": row["source_id"], "item_key": row["item_key"],
            "event_type": row["event_type"], "observed_at": row["observed_at"],
            "detail": _json_load(row["detail_json"], {}),
        } for row in rows]

    def inventory_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT sr.source_id,
                      COUNT(i.item_key) AS total,
                      COALESCE(SUM(CASE WHEN i.item_key IS NOT NULL
                                        AND i.missing_since IS NULL
                                        AND i.retracted_at IS NULL
                                        THEN 1 ELSE 0 END),0) AS active,
                      COALESCE(SUM(CASE WHEN i.missing_since IS NOT NULL
                                        THEN 1 ELSE 0 END),0) AS missing,
                      COALESCE(SUM(CASE WHEN i.retracted_at IS NOT NULL
                                        THEN 1 ELSE 0 END),0) AS retracted
               FROM source_runs sr
               LEFT JOIN items i ON i.source_id=sr.source_id
               WHERE sr.run_id=?
               GROUP BY sr.source_id ORDER BY sr.source_id""",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
