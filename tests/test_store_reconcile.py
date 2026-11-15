"""Bounded-memory observation application, reconciliation modes and checkpoints.

These tests pin the two explicit semantics of ``Store.apply_observation``:

* ``atomic``      – one commit boundary; a mid-write failure leaves the old
                    state entirely in place and exposes no new item;
* ``progressive`` – validated chunks commit early behind coverage checkpoints;
                    unseen items are never marked missing before the full
                    response is confirmed, and an interrupted run resumes from
                    its checkpoint.

They also assert that historical item bodies are never materialized: the
per-source ``items`` table may hold hundreds of thousands of large rows while
a single observation only touches the few keys it actually saw.
"""

from __future__ import annotations

import sqlite3
import tracemalloc

import pytest

from feedkeep.lifecycle import (
    ATOMIC_SNAPSHOT,
    PROGRESSIVE_VISIBLE,
    RECONCILE_MODES,
)
from feedkeep.models import FeedSource, ItemIssue
from feedkeep.store import ResumeError, Store

ALL_MODES = pytest.mark.parametrize("mode", RECONCILE_MODES)


def _source(source_id="s1"):
    return FeedSource(
        id=source_id, name=f"Source {source_id}",
        url="http://127.0.0.1:8765/feeds/rss-basic.xml",
        format="auto", enabled=True, tags=("test",), note="",
    )


def _run(store, started="2024-01-01T00:00:00Z"):
    return store.create_run(started, None)


def _item(item_factory, key_id, **overrides):
    return item_factory(raw_id=key_id, **overrides)


def _seed_history(store, source, run_id, count, body_size=256):
    """Insert ``count`` active archived items with large content bodies."""

    from feedkeep.identity import normalize_item
    from feedkeep.models import ParsedItem

    items = [
        normalize_item(ParsedItem(
            raw_id=f"hist-{index:06d}", title=f"历史 {index}",
            link=f"http://127.0.0.1/hist/{index}",
            summary=None, content="x" * body_size,
            published_at="2023-01-01T00:00:00Z",
        ))
        for index in range(count)
    ]
    return store.apply_observation(
        source.id, run_id, "2023-01-01T00:00:00Z", items, [],
        mode=PROGRESSIVE_VISIBLE, chunk_size=128,
    )


def _item_keys(store, source_id):
    rows = store.conn.execute(
        "SELECT item_key FROM items WHERE source_id=? ORDER BY item_key",
        (source_id,),
    ).fetchall()
    return [row["item_key"] for row in rows]


def _missing_keys(store, source_id):
    rows = store.conn.execute(
        "SELECT item_key FROM items WHERE source_id=? AND missing_since IS NOT NULL "
        "ORDER BY item_key", (source_id,),
    ).fetchall()
    return [row["item_key"] for row in rows]


class SqlTrace:
    """Capture every SQL statement issued while active."""

    def __init__(self, store):
        self.store = store
        self.statements: list[str] = []
        self._token = None

    def __enter__(self):
        self._token = self.store.conn.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc):
        self.store.conn.set_trace_callback(None)

    def normalized(self):
        return [" ".join(sql.split()).upper() for sql in self.statements]

    def select_star_on_items(self):
        return [
            sql for sql in self.normalized()
            if sql.startswith("SELECT *") and " FROM ITEMS" in sql
        ]

    def full_table_item_scans(self):
        """SELECT on items that lacks a key/IN restriction or anti-join guard."""

        scans = []
        for sql in self.normalized():
            if not sql.startswith("SELECT") or " FROM ITEMS" not in sql:
                continue
            if " IN (" in sql or "NOT EXISTS" in sql or "ITEM_KEY=?" in sql:
                continue
            scans.append(sql)
        return scans


# ---------------------------------------------------------------------
# Equivalence with the historical single-transaction behavior
# ---------------------------------------------------------------------

@ALL_MODES
def test_full_observation_matches_legacy_counts_and_events(
    store, item_factory, mode,
):
    source = _source()
    store.sync_sources([source])
    first = _run(store)
    item_a = _item(item_factory, "item-a", title="甲")
    item_b = _item(item_factory, "item-b", title="乙")
    stats = store.apply_observation(
        source.id, first, "2024-01-01T00:00:00Z", [item_a, item_b], [], mode=mode,
    )
    assert (stats.items_seen, stats.items_new, stats.items_updated,
            stats.items_missing, stats.items_restored) == (2, 2, 0, 0, 0)
    assert [event["event_type"] for event in store.get_events(first)] == [
        "discovered", "discovered",
    ]

    second = _run(store, "2024-01-02T00:00:00Z")
    stats = store.apply_observation(
        source.id, second, "2024-01-02T00:00:00Z", [item_a], [], mode=mode,
    )
    assert (stats.items_seen, stats.items_new, stats.items_updated,
            stats.items_missing, stats.items_restored) == (1, 0, 0, 1, 0)
    events = store.get_events(second)
    assert [event["event_type"] for event in events] == ["missing"]
    assert events[0]["item_key"] == item_b.item_key
    assert set(_missing_keys(store, source.id)) == {item_b.item_key}

    third = _run(store, "2024-01-03T00:00:00Z")
    stats = store.apply_observation(
        source.id, third, "2024-01-03T00:00:00Z", [item_a, item_b], [], mode=mode,
    )
    assert (stats.items_seen, stats.items_new, stats.items_updated,
            stats.items_missing, stats.items_restored) == (2, 0, 0, 0, 1)
    assert [event["event_type"] for event in store.get_events(third)] == [
        "restored",
    ]
    assert _missing_keys(store, source.id) == []


@ALL_MODES
def test_truncated_observation_never_marks_missing(store, item_factory, mode):
    source = _source()
    store.sync_sources([source])
    first = _run(store)
    item = _item(item_factory, "item-a")
    store.apply_observation(
        source.id, first, "2024-01-01T00:00:00Z", [item], [], mode=mode,
    )
    second = _run(store, "2024-01-02T00:00:00Z")
    stats = store.apply_observation(
        source.id, second, "2024-01-02T00:00:00Z", [], [],
        truncated=True, mode=mode,
    )
    assert stats.items_missing == 0
    assert store.get_events(second) == []
    assert _missing_keys(store, source.id) == []
    assert store.get_observation_coverage(second) == []


@ALL_MODES
def test_duplicate_item_key_first_wins_silently(store, item_factory, mode):
    """The store boundary keeps the legacy contract: first key wins, and the
    store itself never invents ITEM_DUPLICATE_KEY issues (the ledger does)."""

    source = _source()
    store.sync_sources([source])
    run = _run(store)
    first = _item(item_factory, "dup", title="第一条")
    duplicate = _item(item_factory, "dup", title="第二条，同 key")
    stats = store.apply_observation(
        source.id, run, "2024-01-01T00:00:00Z",
        [first, duplicate, first], [], mode=mode, chunk_size=1,
    )
    assert stats.items_seen == 1
    assert stats.items_new == 1
    assert store.get_item_issues(run) == []
    row = store.conn.execute(
        "SELECT title FROM items WHERE source_id=?", (source.id,)
    ).fetchone()
    assert row["title"] == "第一条"


@ALL_MODES
def test_item_updated_between_chunks_uses_latest_observation(
    store, item_factory, mode,
):
    source = _source()
    store.sync_sources([source])
    run = _run(store)
    old = _item(item_factory, "mover", title="旧标题")
    store.apply_observation(
        source.id, run, "2024-01-01T00:00:00Z", [old], [], mode=mode,
    )

    second = _run(store, "2024-01-02T00:00:00Z")
    # The same key appears twice in one response with different content; the
    # first occurrence is kept, so the archived row must reflect that body.
    first_seen = _item(item_factory, "mover", title="本轮首见标题")
    later_seen = _item(item_factory, "mover", title="本轮后到标题")
    stats = store.apply_observation(
        source.id, second, "2024-01-02T00:00:00Z",
        [first_seen, later_seen], [], mode=mode, chunk_size=1,
    )
    assert stats.items_updated == 1
    row = store.conn.execute(
        "SELECT title, content_hash FROM items WHERE source_id=? AND item_key=?",
        (source.id, first_seen.item_key),
    ).fetchone()
    assert row["title"] == "本轮首见标题"
    assert row["content_hash"] == first_seen.content_hash
    events = store.get_events(second)
    assert len(events) == 1 and events[0]["event_type"] == "updated"


@ALL_MODES
def test_second_restore_does_not_emit_duplicate_event(store, item_factory, mode):
    source = _source()
    store.sync_sources([source])
    item = _item(item_factory, "item-a")
    run1 = _run(store)
    store.apply_observation(
        source.id, run1, "2024-01-01T00:00:00Z", [item], [], mode=mode,
    )
    run2 = _run(store, "2024-01-02T00:00:00Z")
    store.apply_observation(
        source.id, run2, "2024-01-02T00:00:00Z", [], [], mode=mode,
    )
    run3 = _run(store, "2024-01-03T00:00:00Z")
    first_restore = store.apply_observation(
        source.id, run3, "2024-01-03T00:00:00Z", [item], [], mode=mode,
    )
    assert first_restore.items_restored == 1
    run4 = _run(store, "2024-01-04T00:00:00Z")
    again = store.apply_observation(
        source.id, run4, "2024-01-04T00:00:00Z", [item], [], mode=mode,
    )
    assert (again.items_restored, again.items_updated) == (0, 0)
    assert store.get_events(run4) == []
    # Replaying the restoring run itself is a no-op.
    replay = store.apply_observation(
        source.id, run3, "2024-01-03T00:00:00Z", [item], [], mode=mode,
    )
    assert replay == first_restore
    assert len(store.get_events(run3)) == 1


@ALL_MODES
def test_complete_replay_is_idempotent_and_returns_persisted_stats(
    store, item_factory, mode,
):
    source = _source()
    store.sync_sources([source])
    item = _item(item_factory, "item-a")
    run = _run(store)
    first = store.apply_observation(
        source.id, run, "2024-01-01T00:00:00Z", [item],
        [ItemIssue(0, "X", "y")], mode=mode, chunk_size=1,
    )
    second = store.apply_observation(
        source.id, run, "2024-01-01T00:00:00Z", [item],
        [ItemIssue(0, "X", "y")], mode=mode, chunk_size=1,
    )
    assert second == first
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id=?", (source.id,)
    ).fetchone()["n"] == 1
    # Issues belong to the original commit; replay adds none.
    assert len(store.get_item_issues(run)) == 1


# ---------------------------------------------------------------------
# Bounded memory: history bodies are never resident
# ---------------------------------------------------------------------

@ALL_MODES
def test_large_history_small_observation_keeps_bounded_memory(
    store, item_factory, mode,
):
    source = _source()
    store.sync_sources([source])
    history_run = _run(store, "2023-01-01T00:00:00Z")
    history = _seed_history(store, source, history_run, count=400, body_size=20_000)
    assert history.items_new == 400

    observed = [
        _item(item_factory, "observed-1", content="本轮新正文"),
        _item(item_factory, "observed-2", content="本轮另一条"),
    ]
    run = _run(store, "2024-02-01T00:00:00Z")

    with SqlTrace(store) as trace:
        tracemalloc.start()
        stats_before = tracemalloc.take_snapshot()
        result = store.apply_observation(
            source.id, run, "2024-02-01T00:00:00Z", observed, [],
            mode=mode, chunk_size=64,
        )
        stats_after = tracemalloc.take_snapshot()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert result.items_seen == 2
    assert result.items_new == 2
    # 400 historical active items were unseen in a complete observation.
    assert result.items_missing == 400
    # The historical content (~8MB) must never be pulled into Python memory.
    assert peak < 2_000_000
    assert trace.select_star_on_items() == []
    assert trace.full_table_item_scans() == []

    growth = sum(stat.size_diff for stat in stats_after.compare_to(
        stats_before, "lineno"))
    # Resident growth stays an order of magnitude below the archived payload.
    assert growth < 2_000_000


@ALL_MODES
def test_missing_reconciliation_only_reads_keys_and_hashes(
    store, item_factory, mode,
):
    source = _source()
    store.sync_sources([source])
    history_run = _run(store, "2023-01-01T00:00:00Z")
    _seed_history(store, source, history_run, count=200, body_size=10_000)
    run = _run(store, "2024-02-01T00:00:00Z")
    with SqlTrace(store) as trace:
        stats = store.apply_observation(
            source.id, run, "2024-02-01T00:00:00Z", [], [],
            mode=mode, chunk_size=32,
        )
    assert stats.items_missing == 200
    for sql in trace.normalized():
        if " FROM ITEMS" in sql and sql.startswith("SELECT"):
            # Only key + fingerprint columns are streamed; archived bodies,
            # summaries and JSON payloads stay on disk. CONTENT_HASH is allowed.
            for forbidden in (
                "CONTENT,", "CONTENT ", "SUMMARY", "AUTHORS_JSON",
                "CATEGORIES_JSON", "TITLE",
            ):
                assert forbidden not in sql, sql


def test_missing_events_keep_legacy_hash_detail(store, item_factory):
    source = _source()
    store.sync_sources([source])
    item = _item(item_factory, "item-a")
    run1 = _run(store)
    store.apply_observation(
        source.id, run1, "2024-01-01T00:00:00Z", [item], [],
    )
    run2 = _run(store, "2024-01-02T00:00:00Z")
    store.apply_observation(
        source.id, run2, "2024-01-02T00:00:00Z", [], [],
        mode=PROGRESSIVE_VISIBLE, chunk_size=1,
    )
    events = store.get_events(run2)
    assert len(events) == 1
    assert events[0]["event_type"] == "missing"
    assert events[0]["detail"] == {"last_content_hash": item.content_hash}


# ---------------------------------------------------------------------
# Atomic snapshot semantics
# ---------------------------------------------------------------------

def test_atomic_failure_rolls_back_everything(store, item_factory):
    source = _source()
    store.sync_sources([source])
    existing = _item(item_factory, "kept", title="旧条目")
    run1 = _run(store)
    store.apply_observation(
        source.id, run1, "2024-01-01T00:00:00Z", [existing], [],
    )
    before_keys = _item_keys(store, source.id)

    run2 = _run(store, "2024-01-02T00:00:00Z")
    new_items = [
        _item(item_factory, f"new-{index}", content="新内容")
        for index in range(6)
    ]
    # Fail once the finalization writes (missing derivation / checkpoint) run.
    store.fail_writes(lambda sql: "OBSERVATION_CHECKPOINTS" in sql.upper())
    with pytest.raises(RuntimeError, match="injected write failure"):
        store.apply_observation(
            source.id, run2, "2024-01-02T00:00:00Z", new_items, [],
            mode=ATOMIC_SNAPSHOT, chunk_size=2,
        )
    store.fail_writes(None)

    # No new item, no event, no missing marker, no checkpoint, no partial state.
    assert _item_keys(store, source.id) == before_keys
    assert store.get_events(run2) == []
    assert _missing_keys(store, source.id) == []
    assert store.get_observation_coverage(run2) == []
    row = store.conn.execute(
        "SELECT * FROM items WHERE source_id=? AND item_key=?",
        (source.id, existing.item_key),
    ).fetchone()
    assert row["missing_since"] is None
    # The database remains usable: retrying the same observation succeeds.
    retry = store.apply_observation(
        source.id, run2, "2024-01-02T00:00:00Z", new_items, [],
        mode=ATOMIC_SNAPSHOT, chunk_size=2,
    )
    assert retry.items_new == 6
    # The previously kept item was not part of this full response, so it is
    # now missing exactly once rather than being duplicated or dropped.
    assert retry.items_missing == 1
    assert _missing_keys(store, source.id) == [existing.item_key]


# ---------------------------------------------------------------------
# Progressive visibility, checkpoints and resume
# ---------------------------------------------------------------------

def test_progressive_commits_validated_prefix_and_defers_missing(
    store, item_factory,
):
    source = _source()
    store.sync_sources([source])
    history_run = _run(store, "2023-01-01T00:00:00Z")
    _seed_history(store, source, history_run, count=10, body_size=128)
    historical_keys = set(_item_keys(store, source.id))

    run = _run(store, "2024-02-01T00:00:00Z")
    observed = [_item(item_factory, f"obs-{index}") for index in range(5)]

    # Fail during the third chunk transaction (chunk indexes 0, 1 committed).
    calls = {"n": 0}

    def fail_on_third_chunk(sql):
        if "OBSERVATION_CHECKPOINTS" in sql.upper():
            calls["n"] += 1
            if calls["n"] == 3:
                return True
        return False

    store.fail_writes(fail_on_third_chunk)
    with pytest.raises(RuntimeError, match="injected write failure"):
        store.apply_observation(
            source.id, run, "2024-02-01T00:00:00Z", observed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=2,
        )
    store.fail_writes(None)

    # The validated prefix is visible; the failed chunk's items are not.
    visible = set(_item_keys(store, source.id)) - historical_keys
    assert visible == {item.item_key for item in observed[:4]}
    # Crucially, nothing unseen was marked missing while coverage is partial.
    assert _missing_keys(store, source.id) == []
    assert store.get_events(run) and all(
        event["event_type"] == "discovered" for event in store.get_events(run)
    )

    coverage = store.get_observation_coverage(run)
    assert [entry["chunk_index"] for entry in coverage] == [0, 1]
    assert all(entry["chunk_count"] == 3 for entry in coverage)
    assert all(entry["full_response_confirmed"] is False for entry in coverage)
    assert sum(entry["keys_covered"] for entry in coverage) == 4
    assert {entry["reconcile_mode"] for entry in coverage} == {PROGRESSIVE_VISIBLE}

    # Resume with the same response: the prefix is skipped and finalization runs.
    resumed = store.apply_observation(
        source.id, run, "2024-02-01T00:00:00Z", observed, [],
        mode=PROGRESSIVE_VISIBLE, chunk_size=2,
    )
    assert resumed.items_seen == 5
    assert resumed.items_new == 5
    # All ten historical items were absent from the full confirmed response.
    assert resumed.items_missing == 10
    assert store.get_observation_coverage(run) == []
    assert set(_missing_keys(store, source.id)) == historical_keys
    assert len(store.get_events(run)) == 5 + 10


def test_progressive_resume_after_finalization_failure(store, item_factory):
    source = _source()
    store.sync_sources([source])
    history_run = _run(store, "2023-01-01T00:00:00Z")
    _seed_history(store, source, history_run, count=4, body_size=64)
    run = _run(store, "2024-02-01T00:00:00Z")
    observed = [_item(item_factory, f"obs-{index}") for index in range(3)]

    # All chunks commit; the failure happens at the final complete checkpoint.
    checkpoint_calls = {"n": 0}

    def fail_complete(sql):
        if "OBSERVATION_CHECKPOINTS" in sql.upper():
            checkpoint_calls["n"] += 1
            # Chunks 0..2 partial writes first; the complete row is the 4th.
            if checkpoint_calls["n"] == 4:
                return True
        return False

    store.fail_writes(fail_complete)
    with pytest.raises(RuntimeError, match="injected write failure"):
        store.apply_observation(
            source.id, run, "2024-02-01T00:00:00Z", observed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=1,
        )
    store.fail_writes(None)

    # The missing derivation is in the same final transaction, so it rolled back.
    assert _missing_keys(store, source.id) == []
    assert len(store.get_observation_coverage(run)) == 3

    resumed = store.apply_observation(
        source.id, run, "2024-02-01T00:00:00Z", observed, [],
        mode=PROGRESSIVE_VISIBLE, chunk_size=1,
    )
    assert resumed.items_missing == 4
    assert store.get_observation_coverage(run) == []


def test_progressive_resume_rejects_different_mode(store, item_factory):
    source = _source()
    store.sync_sources([source])
    run = _run(store)
    observed = [_item(item_factory, f"obs-{index}") for index in range(4)]
    calls = {"n": 0}

    def fail_second(sql):
        if "OBSERVATION_CHECKPOINTS" in sql.upper():
            calls["n"] += 1
            if calls["n"] == 2:
                return True
        return False

    store.fail_writes(fail_second)
    with pytest.raises(RuntimeError):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", observed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=2,
        )
    store.fail_writes(None)
    with pytest.raises(ResumeError, match="partial 'progressive'"):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", observed, [],
            mode=ATOMIC_SNAPSHOT, chunk_size=2,
        )


def test_progressive_resume_rejects_changed_response(store, item_factory):
    source = _source()
    store.sync_sources([source])
    run = _run(store)
    observed = [_item(item_factory, f"obs-{index}") for index in range(4)]
    calls = {"n": 0}

    def fail_second(sql):
        if "OBSERVATION_CHECKPOINTS" in sql.upper():
            calls["n"] += 1
            if calls["n"] == 2:
                return True
        return False

    store.fail_writes(fail_second)
    with pytest.raises(RuntimeError):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", observed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=2,
        )
    store.fail_writes(None)

    changed = [_item(item_factory, "totally-different-key")]
    with pytest.raises(ResumeError, match="chunks"):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", changed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=2,
        )

    # Same chunk shape but different keys trips the coverage-key guard.
    same_shape = [
        _item(item_factory, "other-key-1"),
        _item(item_factory, "other-key-2"),
        *[_item(item_factory, f"obs-{index}") for index in range(2, 4)],
    ]
    with pytest.raises(ResumeError, match="does not match the observed keys"):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", same_shape, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=2,
        )


def test_progressive_resume_rejects_rechunked_response(store, item_factory):
    source = _source()
    store.sync_sources([source])
    run = _run(store)
    observed = [_item(item_factory, f"obs-{index}") for index in range(4)]
    calls = {"n": 0}

    def fail_second(sql):
        if "OBSERVATION_CHECKPOINTS" in sql.upper():
            calls["n"] += 1
            if calls["n"] == 2:
                return True
        return False

    store.fail_writes(fail_second)
    with pytest.raises(RuntimeError):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", observed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=2,
        )
    store.fail_writes(None)
    with pytest.raises(ResumeError, match="chunks"):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", observed, [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=1,
        )


def test_progressive_truncated_response_completes_without_missing(
    store, item_factory,
):
    source = _source()
    store.sync_sources([source])
    history_run = _run(store, "2023-01-01T00:00:00Z")
    _seed_history(store, source, history_run, count=6, body_size=64)
    run = _run(store, "2024-02-01T00:00:00Z")
    observed = [_item(item_factory, f"obs-{index}") for index in range(3)]
    stats = store.apply_observation(
        source.id, run, "2024-02-01T00:00:00Z", observed, [],
        truncated=True, mode=PROGRESSIVE_VISIBLE, chunk_size=1,
    )
    assert stats.items_missing == 0
    assert _missing_keys(store, source.id) == []
    assert store.get_observation_coverage(run) == []


def test_invalid_arguments_are_rejected(store, item_factory):
    source = _source()
    store.sync_sources([source])
    run = _run(store)
    item = _item(item_factory, "item-a")
    with pytest.raises(ValueError, match="reconcile mode"):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", [item], [], mode="bogus",
        )
    with pytest.raises(ValueError, match="chunk_size"):
        store.apply_observation(
            source.id, run, "2024-01-01T00:00:00Z", [item], [], chunk_size=0,
        )


# ---------------------------------------------------------------------
# Schema migration from the historical v1 layout
# ---------------------------------------------------------------------

def test_v1_database_migrates_and_keeps_history(tmp_path, item_factory):
    import feedkeep.schema as schema_mod

    db_path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(db_path)
    # Reconstruct a genuine version-1 database from the committed schema.
    v1_sql = (
        "CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta(key,value) VALUES('schema_version','1');"
        "CREATE TABLE sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,"
        "format TEXT NOT NULL,enabled INTEGER NOT NULL,tags_json TEXT NOT NULL,"
        "note TEXT NOT NULL,etag TEXT,last_modified TEXT,last_checked_at TEXT,"
        "last_success_at TEXT,consecutive_failures INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,"
        "status TEXT NOT NULL,config_path TEXT,sources_total INTEGER DEFAULT 0,"
        "sources_ok INTEGER DEFAULT 0,sources_not_modified INTEGER DEFAULT 0,"
        "sources_failed INTEGER DEFAULT 0,items_seen INTEGER DEFAULT 0,"
        "items_new INTEGER DEFAULT 0,items_updated INTEGER DEFAULT 0,"
        "items_missing INTEGER DEFAULT 0,items_restored INTEGER DEFAULT 0,"
        "item_errors INTEGER DEFAULT 0);"
        "CREATE TABLE source_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "run_id TEXT NOT NULL,source_id TEXT NOT NULL,source_name TEXT NOT NULL,"
        "status TEXT NOT NULL,http_status INTEGER,duration_ms INTEGER NOT NULL,"
        "body_bytes INTEGER NOT NULL,body_sha256 TEXT,etag TEXT,last_modified TEXT,"
        "attempts INTEGER NOT NULL,items_total INTEGER DEFAULT 0,items_new INTEGER DEFAULT 0,"
        "items_updated INTEGER DEFAULT 0,items_missing INTEGER DEFAULT 0,"
        "items_restored INTEGER DEFAULT 0,item_errors INTEGER DEFAULT 0,"
        "error_code TEXT,error_message TEXT,truncated INTEGER NOT NULL DEFAULT 0,"
        "observed_at TEXT NOT NULL,UNIQUE(run_id,source_id));"
        "CREATE TABLE items(source_id TEXT NOT NULL,item_key TEXT NOT NULL,raw_id TEXT,"
        "title TEXT,link TEXT,summary TEXT,content TEXT,published_at TEXT,"
        "authors_json TEXT NOT NULL,categories_json TEXT NOT NULL,content_hash TEXT NOT NULL,"
        "first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,last_changed_at TEXT,"
        "missing_since TEXT,first_run_id TEXT NOT NULL,last_run_id TEXT NOT NULL,"
        "PRIMARY KEY(source_id,item_key));"
        "CREATE TABLE item_enclosures(source_id TEXT NOT NULL,item_key TEXT NOT NULL,"
        "position INTEGER NOT NULL,url TEXT NOT NULL,mime_type TEXT,declared_length INTEGER,"
        "PRIMARY KEY(source_id,item_key,position));"
        "CREATE TABLE item_events(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT NOT NULL,"
        "item_key TEXT NOT NULL,run_id TEXT NOT NULL,event_type TEXT NOT NULL,"
        "observed_at TEXT NOT NULL,detail_json TEXT NOT NULL,"
        "UNIQUE(source_id,item_key,run_id,event_type));"
        "CREATE TABLE item_issues(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,"
        "source_id TEXT NOT NULL,item_index INTEGER NOT NULL,code TEXT NOT NULL,"
        "message TEXT NOT NULL);"
        "CREATE INDEX idx_items_source_missing ON items(source_id,missing_since);"
        "CREATE INDEX idx_events_run ON item_events(run_id);"
        "CREATE INDEX idx_issues_run ON item_issues(run_id);"
    )
    conn.executescript(v1_sql)
    conn.execute(
        "INSERT INTO sources VALUES('s1','n','http://127.0.0.1/f','rss',1,'[]','',"
        "NULL,NULL,NULL,NULL,0)"
    )
    conn.execute(
        "INSERT INTO runs VALUES('run-000001','2024-01-01T00:00:00Z',NULL,'running',NULL,"
        "0,0,0,0,0,0,0,0,0,0)"
    )
    conn.execute(
        "INSERT INTO items(source_id,item_key,raw_id,title,link,summary,content,"
        "published_at,authors_json,categories_json,content_hash,first_seen_at,"
        "last_seen_at,last_changed_at,missing_since,first_run_id,last_run_id) "
        "VALUES('s1','k1','r1','t',NULL,NULL,NULL,NULL,'[]','[]','h1',"
        "'2024-01-01T00:00:00Z','2024-01-01T00:00:00Z',NULL,NULL,"
        "'run-000001','run-000001')"
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        assert schema_mod.SCHEMA_VERSION == 2
        version = store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert version == "2"
        columns = {
            row["name"] for row in store.conn.execute(
                "PRAGMA table_info(source_runs)"
            ).fetchall()
        }
        assert "reconcile_mode" in columns
        store.conn.execute(
            "SELECT chunk_index,status FROM observation_checkpoints LIMIT 0"
        )
        # Existing history survives and new observations work in both modes.
        item = item_factory(raw_id="r1")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE items SET item_key=? WHERE source_id='s1' AND item_key='k1'",
            (item.item_key,),
        )
        conn.commit()
        conn.close()
        run2 = store.create_run("2024-02-01T00:00:00Z", None)
        stats = store.apply_observation(
            "s1", run2, "2024-02-01T00:00:00Z", [item], [],
            mode=PROGRESSIVE_VISIBLE, chunk_size=1,
        )
        assert stats.items_seen == 1 and stats.items_new == 0
    finally:
        store.close()
