"""SQLite archive: idempotent observations, lifecycle events and audit queries.

Observation application never materializes a source's full history. Existing
items are looked up in bounded key batches, and the missing derivation streams
primary keys instead of loading archived bodies. Two explicit reconciliation
semantics are supported (see :mod:`feedkeep.lifecycle`):

* ``atomic``      – one transaction, all-or-nothing visibility;
* ``progressive`` – validated chunks commit early, coverage is checkpointed,
                    and unseen items are never marked missing until the full
                    response has been confirmed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .lifecycle import (
    ATOMIC_SNAPSHOT,
    PROGRESSIVE_VISIBLE,
    plan_missing_for_hash,
    plan_transition,
    stage_observation,
    validate_reconcile_mode,
)
from .models import (ApplyStats, Enclosure, ExistingItem, FeedSource, ItemIssue,
                     NormalizedItem, RunCounts, SourceRunRecord, SourceState)
from .schema import initialize

# Default number of observed items processed (and existing rows fetched) per
# bounded batch. Resident memory is O(chunk_size + response keys), never
# O(history size).
DEFAULT_CHUNK_SIZE = 500

# chunk_index used by the single checkpoint row that marks a fully confirmed
# observation, including the finalized missing reconciliation.
COMPLETE_INDEX = -1

# Only the columns required to plan a transition / rewrite an item row. This is
# deliberately explicit so no ``SELECT *`` can drag archived bodies back in.
_EXISTING_COLUMNS = (
    "source_id,item_key,title,link,summary,content,published_at,"
    "authors_json,categories_json,content_hash,missing_since,last_changed_at"
)


class ResumeError(RuntimeError):
    """Raised when a progressive observation cannot continue from its checkpoint.

    The partially applied prefix remains committed and is visible through
    :meth:`Store.get_observation_coverage`; no missing/restored bookkeeping is
    performed for the interrupted run.
    """


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
        # Test seam: predicate(statement_text) -> truthy raises before the
        # write reaches SQLite, simulating a mid-write failure.
        self._write_failure: Optional[Callable[[str], bool]] = None

    def fail_writes(self, predicate: Optional[Callable[[str], bool]]) -> None:
        """Install (or clear) a predicate that forces write failures in tests."""

        self._write_failure = predicate

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
                self._write(
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
            self._write(
                """UPDATE sources SET last_checked_at=?, last_success_at=?,
                   consecutive_failures=0, etag=COALESCE(?, etag),
                   last_modified=COALESCE(?, last_modified) WHERE id=?""",
                (checked_at, checked_at, etag, last_modified, source_id),
            )

    def mark_success(self, source_id: str, checked_at: str, etag: Optional[str],
                     last_modified: Optional[str]) -> None:
        with self.conn:
            self._write(
                """UPDATE sources SET last_checked_at=?, last_success_at=?,
                   consecutive_failures=0, etag=?, last_modified=? WHERE id=?""",
                (checked_at, checked_at, etag, last_modified, source_id),
            )

    def mark_failure(self, source_id: str, checked_at: str) -> None:
        with self.conn:
            self._write(
                "UPDATE sources SET last_checked_at=?, consecutive_failures=consecutive_failures+1 "
                "WHERE id=?", (checked_at, source_id))

    # -- runs ---------------------------------------------------------
    def create_run(self, started_at: str, config_path: Optional[str]) -> str:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(CAST(substr(id, 5) AS INTEGER)), 0) AS n FROM runs"
        ).fetchone()
        run_id = f"run-{int(row['n']) + 1:06d}"
        with self.conn:
            self._write(
                "INSERT INTO runs(id,started_at,status,config_path) VALUES(?,?,?,?)",
                (run_id, started_at, "running", config_path),
            )
        return run_id

    def finish_run(self, run_id: str, finished_at: str, status: str,
                   counts: RunCounts) -> None:
        columns = ", ".join(f"{key}=?" for key in counts.as_dict())
        with self.conn:
            self._write(
                f"UPDATE runs SET finished_at=?, status=?, {columns} WHERE id=?",
                (finished_at, status, *counts.as_dict().values(), run_id),
            )

    def add_source_run(self, run_id: str, record: SourceRunRecord) -> None:
        with self.conn:
            self._write(
                """INSERT INTO source_runs(
                   run_id,source_id,source_name,status,http_status,duration_ms,
                   body_bytes,body_sha256,etag,last_modified,attempts,items_total,
                   items_new,items_updated,items_missing,items_restored,item_errors,
                   error_code,error_message,truncated,reconcile_mode,observed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, record.source_id, record.source_name, record.status,
                 record.http_status, record.duration_ms, record.body_bytes,
                 record.body_sha256, record.etag, record.last_modified, record.attempts,
                 record.items_total, record.items_new, record.items_updated,
                 record.items_missing, record.items_restored, record.item_errors,
                 record.error_code, record.error_message, int(record.truncated),
                 record.reconcile_mode, record.observed_at),
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
        )

    def _insert_event(self, source_id: str, item_key: str, run_id: str,
                      observed_at: str, event: Any) -> None:
        self._write(
            """INSERT OR IGNORE INTO item_events(
               source_id,item_key,run_id,event_type,observed_at,detail_json)
               VALUES(?,?,?,?,?,?)""",
            (source_id, item_key, run_id, event.event_type, observed_at,
             _json(dict(event.detail))),
        )

    def _replace_enclosures(self, source_id: str, item_key: str,
                            item: NormalizedItem) -> None:
        self._write(
            "DELETE FROM item_enclosures WHERE source_id=? AND item_key=?",
            (source_id, item_key),
        )
        self._write_many(
            """INSERT INTO item_enclosures(
               source_id,item_key,position,url,mime_type,declared_length)
               VALUES(?,?,?,?,?,?)""",
            [(source_id, item_key, enc.position, enc.url, enc.mime_type, enc.declared_length)
             for enc in item.enclosures],
        )

    def _fetch_existing(self, source_id: str,
                        keys: Sequence[str]) -> dict[str, sqlite3.Row]:
        """Fetch archived rows for one bounded key batch only."""

        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = self.conn.execute(
            f"SELECT {_EXISTING_COLUMNS} FROM items "
            f"WHERE source_id=? AND item_key IN ({placeholders})",
            (source_id, *keys),
        ).fetchall()
        return {row["item_key"]: row for row in rows}

    def _upsert_item(self, source_id: str, run_id: str, observed_at: str,
                     item: NormalizedItem, row: Optional[sqlite3.Row]) -> ApplyStats:
        stats = ApplyStats(items_seen=1)
        existing = self._existing(row) if row is not None else None
        transition = plan_transition(existing, item)
        values = (item.raw_id, item.title, item.link, item.summary, item.content,
                  item.published_at, _json(list(item.authors)),
                  _json(list(item.categories)), item.content_hash)
        if existing is None:
            self._write(
                """INSERT INTO items(
                   source_id,item_key,raw_id,title,link,summary,content,published_at,
                   authors_json,categories_json,content_hash,first_seen_at,last_seen_at,
                   last_changed_at,missing_since,first_run_id,last_run_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (source_id, item.item_key, *values, observed_at, observed_at,
                 observed_at, run_id, run_id),
            )
            stats.items_new = 1
        else:
            last_changed = observed_at if transition.is_updated else row["last_changed_at"]
            self._write(
                """UPDATE items SET raw_id=?,title=?,link=?,summary=?,content=?,
                   published_at=?,authors_json=?,categories_json=?,content_hash=?,
                   last_seen_at=?,last_changed_at=?,missing_since=NULL,last_run_id=?
                   WHERE source_id=? AND item_key=?""",
                (*values, observed_at, last_changed, run_id, source_id, item.item_key),
            )
            stats.items_updated = int(transition.is_updated)
            stats.items_restored = int(transition.is_restored)
        self._replace_enclosures(source_id, item.item_key, item)
        for event in transition.events:
            self._insert_event(source_id, item.item_key, run_id, observed_at, event)
        return stats

    def _apply_chunk(self, source_id: str, run_id: str, observed_at: str,
                     chunk: Sequence[NormalizedItem]) -> ApplyStats:
        """Apply one bounded batch; callers own the transaction boundary."""

        stats = ApplyStats()
        rows = self._fetch_existing(source_id, [item.item_key for item in chunk])
        for item in chunk:
            stats.add(self._upsert_item(
                source_id, run_id, observed_at, item, rows.get(item.item_key),
            ))
        return stats

    def _reconcile_missing(self, source_id: str, run_id: str, observed_at: str,
                           seen_keys: Sequence[str], chunk_size: int) -> int:
        """Mark unseen active items missing via a streaming key-only anti-join.

        A temp table holds the bounded staging key set; the archived content
        columns are never read. Rows are consumed and written in sorted batches
        so the emitted event order matches the historical full-scan behavior.
        """

        temp_table = "tmp_seen_keys"
        self.conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
        self.conn.execute(
            f"CREATE TEMP TABLE {temp_table}(item_key TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        try:
            self.conn.executemany(
                f"INSERT INTO {temp_table}(item_key) VALUES(?)",
                [(key,) for key in seen_keys],
            )
            cursor = self.conn.execute(
                f"""SELECT i.item_key, i.content_hash FROM items i
                    WHERE i.source_id=? AND i.missing_since IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM {temp_table} t WHERE t.item_key = i.item_key
                      )
                    ORDER BY i.item_key""",
                (source_id,),
            )
            marked = 0
            while True:
                batch = cursor.fetchmany(chunk_size)
                if not batch:
                    break
                self._write_many(
                    "UPDATE items SET missing_since=? WHERE source_id=? AND item_key=?",
                    [(observed_at, source_id, row["item_key"]) for row in batch],
                )
                self._write_many(
                    """INSERT OR IGNORE INTO item_events(
                       source_id,item_key,run_id,event_type,observed_at,detail_json)
                       VALUES(?,?,?,?,?,?)""",
                    [
                        (source_id, row["item_key"], run_id, "missing", observed_at,
                         _json(dict(plan_missing_for_hash(row["content_hash"]).detail)))
                        for row in batch
                    ],
                )
                marked += len(batch)
            return marked
        finally:
            self.conn.execute(f"DROP TABLE IF EXISTS {temp_table}")

    def _insert_issues(self, run_id: str, source_id: str,
                       issues: Sequence[ItemIssue]) -> None:
        self._write_many(
            """INSERT INTO item_issues(run_id,source_id,item_index,code,message)
               VALUES(?,?,?,?,?)""",
            [(run_id, source_id, issue.index, issue.code, issue.message)
             for issue in issues],
        )

    # -- coverage checkpoints ----------------------------------------
    def _checkpoint_row(self, source_id: str, run_id: str,
                        chunk_index: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM observation_checkpoints
               WHERE source_id=? AND run_id=? AND chunk_index=?""",
            (source_id, run_id, chunk_index),
        ).fetchone()

    def _write_checkpoint(self, source_id: str, run_id: str, chunk_index: int,
                          status: str, mode: str, chunk_count: int,
                          keys: Sequence[str], stats: ApplyStats, truncated: bool,
                          full_response: bool, observed_at: str) -> None:
        self._write(
            """INSERT INTO observation_checkpoints(
               source_id,run_id,chunk_index,status,reconcile_mode,chunk_count,
               keys_json,items_seen,items_new,items_updated,items_missing,
               items_restored,item_errors,truncated,full_response,observed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id,run_id,chunk_index) DO UPDATE SET
                 status=excluded.status,
                 reconcile_mode=excluded.reconcile_mode,
                 chunk_count=excluded.chunk_count,
                 keys_json=excluded.keys_json,
                 items_seen=excluded.items_seen,
                 items_new=excluded.items_new,
                 items_updated=excluded.items_updated,
                 items_missing=excluded.items_missing,
                 items_restored=excluded.items_restored,
                 item_errors=excluded.item_errors,
                 truncated=excluded.truncated,
                 full_response=excluded.full_response,
                 observed_at=excluded.observed_at""",
            (source_id, run_id, chunk_index, status, mode, chunk_count,
             _json(list(keys)), stats.items_seen, stats.items_new, stats.items_updated,
             stats.items_missing, stats.items_restored, stats.item_errors,
             int(truncated), int(full_response), observed_at),
        )

    @staticmethod
    def _stats_from_checkpoint(row: sqlite3.Row) -> ApplyStats:
        return ApplyStats(
            items_seen=row["items_seen"], items_new=row["items_new"],
            items_updated=row["items_updated"], items_missing=row["items_missing"],
            items_restored=row["items_restored"], item_errors=row["item_errors"],
        )

    def get_observation_coverage(self,
                                 run_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Return unfinished progressive coverage (partial checkpoints).

        Complete observations are not listed: their result is already part of
        the run's source record. Each entry describes one committed, validated
        prefix whose unseen items were deliberately *not* marked missing.
        """

        if run_id is None:
            rows = self.conn.execute(
                """SELECT * FROM observation_checkpoints WHERE status='partial'
                   ORDER BY run_id, source_id, chunk_index"""
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM observation_checkpoints WHERE status='partial'
                   AND run_id=? ORDER BY source_id, chunk_index""",
                (run_id,),
            ).fetchall()
        return [{
            "run_id": row["run_id"],
            "source_id": row["source_id"],
            "chunk_index": row["chunk_index"],
            "chunk_count": row["chunk_count"],
            "reconcile_mode": row["reconcile_mode"],
            "keys_covered": len(_json_load(row["keys_json"], [])),
            "truncated": bool(row["truncated"]),
            "full_response_confirmed": bool(row["full_response"]),
            "observed_at": row["observed_at"],
        } for row in rows]

    # -- observation entry points ------------------------------------
    def apply_observation(self, source_id: str, run_id: str, observed_at: str,
                          items: Sequence[NormalizedItem], issues: Sequence[ItemIssue],
                          truncated: bool = False,
                          mode: str = ATOMIC_SNAPSHOT,
                          chunk_size: int = DEFAULT_CHUNK_SIZE) -> ApplyStats:
        validate_reconcile_mode(mode)
        if not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")

        # First occurrence wins; duplicate-key issues are recorded by the
        # ledger before this boundary (legacy behavior preserved).
        unique = stage_observation(items)
        all_issues = list(issues)
        chunks = [unique[start:start + chunk_size]
                  for start in range(0, len(unique), chunk_size)]

        # A confirmed observation is fully idempotent under replay, in either
        # mode: return the persisted counters without touching item rows.
        complete = self._checkpoint_row(source_id, run_id, COMPLETE_INDEX)
        if complete is not None:
            return self._stats_from_checkpoint(complete)

        partial = self.conn.execute(
            """SELECT * FROM observation_checkpoints
               WHERE source_id=? AND run_id=? AND status='partial'
               ORDER BY chunk_index""",
            (source_id, run_id),
        ).fetchall()
        if partial:
            stored_mode = partial[0]["reconcile_mode"]
            if stored_mode != mode:
                raise ResumeError(
                    f"source {source_id!r} has a partial {stored_mode!r} observation "
                    f"for run {run_id!r}; resume with mode={stored_mode!r} or finish it first"
                )
            return self._apply_progressive(
                source_id, run_id, observed_at, chunks, all_issues,
                truncated, chunk_size, partial,
            )

        if mode == ATOMIC_SNAPSHOT:
            return self._apply_atomic(
                source_id, run_id, observed_at, unique, chunks, all_issues,
                truncated, chunk_size,
            )
        return self._apply_progressive(
            source_id, run_id, observed_at, chunks, all_issues,
            truncated, chunk_size, (),
        )

    def _apply_atomic(self, source_id: str, run_id: str, observed_at: str,
                      unique: Sequence[NormalizedItem],
                      chunks: Sequence[Sequence[NormalizedItem]],
                      issues: Sequence[ItemIssue], truncated: bool,
                      chunk_size: int) -> ApplyStats:
        """Single commit boundary: any failure rolls the whole observation back."""

        stats = ApplyStats()
        with self.conn:
            for chunk in chunks:
                stats.add(self._apply_chunk(source_id, run_id, observed_at, chunk))
            if not truncated:
                stats.items_missing += self._reconcile_missing(
                    source_id, run_id, observed_at,
                    [item.item_key for item in unique], chunk_size,
                )
            self._insert_issues(run_id, source_id, issues)
            stats.item_errors = len(issues)
            self._write_checkpoint(
                source_id, run_id, COMPLETE_INDEX, "complete", ATOMIC_SNAPSHOT,
                len(chunks), [item.item_key for item in unique], stats,
                truncated, full_response=not truncated, observed_at=observed_at,
            )
        return stats

    def _apply_progressive(self, source_id: str, run_id: str, observed_at: str,
                           chunks: Sequence[Sequence[NormalizedItem]],
                           issues: Sequence[ItemIssue], truncated: bool,
                           chunk_size: int,
                           existing_partials: Sequence[sqlite3.Row]) -> ApplyStats:
        """Commit validated chunks early; missing work waits for full coverage."""

        done = {row["chunk_index"]: row for row in existing_partials}
        stats = ApplyStats()
        for index, row in sorted(done.items()):
            if row["chunk_count"] != len(chunks):
                raise ResumeError(
                    f"checkpoint for source {source_id!r} run {run_id!r} chunk {index} "
                    f"expected {row['chunk_count']} chunks but the response now forms "
                    f"{len(chunks)}"
                )
            stored_keys = _json_load(row["keys_json"], [])
            chunk_keys = [item.item_key for item in chunks[index]]
            if stored_keys != chunk_keys:
                raise ResumeError(
                    f"checkpoint for source {source_id!r} run {run_id!r} chunk {index} "
                    "does not match the observed keys; refusing to resume a different "
                    "response over a partial state"
                )
            stats.add(self._stats_from_checkpoint(row))

        for index, chunk in enumerate(chunks):
            if index in done:
                continue
            with self.conn:
                chunk_stats = self._apply_chunk(
                    source_id, run_id, observed_at, chunk,
                )
                # The checkpoint is part of the same commit as the chunk:
                # either both are visible or neither is.
                self._write_checkpoint(
                    source_id, run_id, index, "partial", PROGRESSIVE_VISIBLE,
                    len(chunks), [item.item_key for item in chunk], chunk_stats,
                    truncated, full_response=False, observed_at=observed_at,
                )
            stats.add(chunk_stats)

        # Full response confirmed: only now may unseen items be marked missing.
        with self.conn:
            if not truncated:
                seen_keys: list[str] = []
                for chunk in chunks:
                    seen_keys.extend(item.item_key for item in chunk)
                stats.items_missing += self._reconcile_missing(
                    source_id, run_id, observed_at, seen_keys, chunk_size,
                )
            self._insert_issues(run_id, source_id, issues)
            stats.item_errors = len(issues)
            all_keys = [item.item_key for chunk in chunks for item in chunk]
            self._write_checkpoint(
                source_id, run_id, COMPLETE_INDEX, "complete",
                PROGRESSIVE_VISIBLE, len(chunks), all_keys, stats,
                truncated, full_response=not truncated, observed_at=observed_at,
            )
            # Coverage is now fully confirmed: drop the per-chunk prefix rows
            # so the store no longer reports a partial observation.
            self._write(
                "DELETE FROM observation_checkpoints "
                "WHERE source_id=? AND run_id=? AND status='partial'",
                (source_id, run_id),
            )
        return stats

    # -- write seam ---------------------------------------------------
    def _write(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        if self._write_failure is not None and self._write_failure(sql):
            raise RuntimeError("injected write failure")
        return self.conn.execute(sql, params)

    def _write_many(self, sql: str, seq: Any) -> sqlite3.Cursor:
        if self._write_failure is not None and self._write_failure(sql):
            raise RuntimeError("injected write failure")
        return self.conn.executemany(sql, seq)

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
                      COALESCE(SUM(CASE WHEN i.item_key IS NOT NULL AND i.missing_since IS NULL
                                        THEN 1 ELSE 0 END),0) AS active,
                      COALESCE(SUM(CASE WHEN i.missing_since IS NOT NULL
                                        THEN 1 ELSE 0 END),0) AS missing
               FROM source_runs sr
               LEFT JOIN items i ON i.source_id=sr.source_id
               WHERE sr.run_id=?
               GROUP BY sr.source_id ORDER BY sr.source_id""",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
