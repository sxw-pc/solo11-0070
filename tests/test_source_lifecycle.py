"""Source lifecycle: explicit pause / retire / resume with archive epochs."""

from __future__ import annotations

import json
import sqlite3

import pytest

from feedkeep.cli import main
from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.models import FeedSource, RunCounts, SourceRunRecord
from feedkeep.report import build_report, render_markdown
from feedkeep.store import SourceLifecycleError, Store

T0 = "2024-03-01T00:00:00Z"


def _ledger(store, policy, clock):
    return Ledger(store, policy, Fetcher(policy, clock), clock)


def _enabled(source: FeedSource) -> FeedSource:
    return FeedSource(
        id=source.id, name=source.name, url=source.url, format=source.format,
        enabled=True, tags=source.tags, note=source.note,
    )


def _source_run_record(source_id: str, run_id: str, observed_at: str) -> SourceRunRecord:
    return SourceRunRecord(
        source_id=source_id, source_name=source_id, status="ok", http_status=200,
        duration_ms=1, body_bytes=1, body_sha256=None, etag=None, last_modified=None,
        attempts=1, items_total=0, items_new=0, items_updated=0, items_missing=0,
        items_restored=0, item_errors=0, error_code=None, error_message=None,
        truncated=False, observed_at=observed_at,
    )


# -- pause ---------------------------------------------------------------

def test_pause_stops_fetching_and_keeps_items_active(store, mock_server, fixed_clock,
                                                     source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    first = ledger.run([source])
    assert first.counts.items_new == 3
    mock_server.reset()

    fixed_clock.advance(60)
    result = store.pause_source(source.id, fixed_clock.now_iso())
    assert result.changed is True
    assert result.enabled is False
    assert result.event.event_type == "paused"
    assert result.event.epoch_id == 1

    fixed_clock.advance(60)
    second = ledger.run([_enabled(source)])
    assert second.counts.sources_total == 0
    assert second.counts.items_missing == 0
    assert mock_server.snapshot() == []
    assert store.get_source_runs(second.run_id) == []
    assert store.get_events(second.run_id) == []

    rows = store.conn.execute(
        "SELECT missing_since FROM items WHERE source_id=?", (source.id,)
    ).fetchall()
    assert len(rows) == 3 and all(row["missing_since"] is None for row in rows)

    inventory = store.inventory_for_run(second.run_id)
    assert inventory == [{
        "source_id": source.id, "epoch_id": 1, "lifecycle": "paused",
        "total": 3, "active": 3, "missing": 0, "retired": 0,
    }]


def test_pause_resume_continues_same_epoch(store, mock_server, fixed_clock,
                                           source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    ledger.run([source])
    fixed_clock.advance(60)
    store.pause_source(source.id, fixed_clock.now_iso())
    fixed_clock.advance(60)
    resumed = store.resume_source(source.id, fixed_clock.now_iso())
    assert resumed.changed is True
    assert resumed.enabled is True
    assert resumed.event.event_type == "resumed"
    assert resumed.event.epoch_id == 1

    fixed_clock.advance(60)
    summary = ledger.run([_enabled(source)])
    assert summary.counts.sources_total == 1
    assert summary.counts.items_new == 0
    assert summary.counts.items_missing == 0
    assert store.inventory_for_run(summary.run_id)[0]["active"] == 3


def test_pause_is_idempotent_and_rejects_retired(store, mock_server, fixed_clock,
                                                 source_factory, default_policy):
    source = source_factory(mock_server)
    _ledger(store, default_policy, fixed_clock).run([source])
    fixed_clock.advance(60)
    store.pause_source(source.id, fixed_clock.now_iso())
    fixed_clock.advance(60)
    again = store.pause_source(source.id, fixed_clock.now_iso())
    assert again.changed is False
    assert again.event is None
    assert [e["event_type"] for e in store.get_source_events()] == ["paused"]

    fixed_clock.advance(60)
    store.retire_source(source.id, fixed_clock.now_iso())
    with pytest.raises(SourceLifecycleError):
        store.pause_source(source.id, fixed_clock.now_iso())


# -- retire ---------------------------------------------------------------

def test_retire_moves_items_to_retired_inventory(store, mock_server, fixed_clock,
                                                 source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    first = ledger.run([source])
    mock_server.reset()

    fixed_clock.advance(60)
    result = store.retire_source(source.id, fixed_clock.now_iso())
    assert result.changed is True
    assert result.enabled is False
    assert result.retired_at == fixed_clock.now_iso()
    assert result.event.event_type == "retired"
    assert result.event.detail == {"items_retired": 3}

    fixed_clock.advance(60)
    second = ledger.run([_enabled(source)])
    # 退役不伪装成一次成功的空 feed：没有 source_run、没有 missing、没有条目事件
    assert second.counts.sources_total == 0
    assert second.counts.items_missing == 0
    assert mock_server.snapshot() == []
    assert store.get_source_runs(second.run_id) == []
    assert store.get_events(second.run_id) == []
    rows = store.conn.execute(
        "SELECT missing_since FROM items WHERE source_id=?", (source.id,)
    ).fetchall()
    assert all(row["missing_since"] is None for row in rows)

    current = store.inventory_for_run(second.run_id)
    assert current == [{
        "source_id": source.id, "epoch_id": 1, "lifecycle": "retired",
        "total": 3, "active": 0, "missing": 0, "retired": 3,
    }]
    # 历史 run 的库存按发生时点查询：退役前 alpha/beta/... 仍是活跃的
    historical = store.inventory_for_run(first.run_id)
    assert historical == [{
        "source_id": source.id, "epoch_id": 1, "lifecycle": "active",
        "total": 3, "active": 3, "missing": 0, "retired": 0,
    }]


def test_retire_is_idempotent(store, mock_server, fixed_clock,
                              source_factory, default_policy):
    source = source_factory(mock_server)
    _ledger(store, default_policy, fixed_clock).run([source])
    fixed_clock.advance(60)
    first = store.retire_source(source.id, fixed_clock.now_iso())
    fixed_clock.advance(60)
    second = store.retire_source(source.id, fixed_clock.now_iso())
    assert second.changed is False
    assert second.event is None
    assert second.retired_at == first.retired_at
    assert [e["event_type"] for e in store.get_source_events()] == ["retired"]


def test_same_second_retire_does_not_rewrite_historical_inventory(
        store, mock_server, fixed_clock, source_factory, default_policy):
    # 不推进时钟：run 结束与退役落在同一秒。库存按 run 边界（逻辑时序）
    # 而非墙钟秒判定，历史 run 的清单不受之后退役影响。
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    first = ledger.run([source])
    result = store.retire_source(source.id, fixed_clock.now_iso())
    assert result.event.last_run_id == first.run_id

    historical = store.inventory_for_run(first.run_id)
    assert historical[0]["lifecycle"] == "active"
    assert historical[0]["active"] == 3
    assert historical[0]["retired"] == 0

    second = ledger.run([_enabled(source)])
    current = store.inventory_for_run(second.run_id)
    assert current[0]["lifecycle"] == "retired"
    assert current[0]["retired"] == 3


def test_retire_unknown_source_raises(store):
    with pytest.raises(SourceLifecycleError):
        store.retire_source("nope", T0)
    with pytest.raises(SourceLifecycleError):
        store.pause_source("nope", T0)
    with pytest.raises(SourceLifecycleError):
        store.resume_source("nope", T0)


def test_config_sync_cannot_resurrect_retired(store, mock_server, fixed_clock,
                                              source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    ledger.run([source])
    fixed_clock.advance(60)
    store.retire_source(source.id, fixed_clock.now_iso())

    blocked = store.sync_sources([_enabled(source)])
    assert source.id in blocked
    row = store.conn.execute(
        "SELECT retired_at, epoch_id FROM sources WHERE id=?", (source.id,)
    ).fetchone()
    assert row["retired_at"] is not None
    assert row["epoch_id"] == 1

    fixed_clock.advance(60)
    mock_server.reset()
    summary = ledger.run([_enabled(source)])
    assert summary.counts.sources_total == 0
    assert mock_server.snapshot() == []


def test_apply_observation_rejects_retired_source(store, mock_server, fixed_clock,
                                                  source_factory, default_policy,
                                                  item_factory):
    source = source_factory(mock_server)
    _ledger(store, default_policy, fixed_clock).run([source])
    fixed_clock.advance(60)
    store.retire_source(source.id, fixed_clock.now_iso())
    run_id = store.create_run(fixed_clock.now_iso(), None)
    with pytest.raises(SourceLifecycleError):
        store.apply_observation(source.id, run_id, fixed_clock.now_iso(),
                                [item_factory()], [])


# -- resume ---------------------------------------------------------------

def test_resume_epoch_restores_retired_items(store, mock_server, fixed_clock,
                                             source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    ledger.run([source])
    fixed_clock.advance(60)
    store.retire_source(source.id, fixed_clock.now_iso())
    fixed_clock.advance(60)
    resumed = store.resume_source(source.id, fixed_clock.now_iso(), mode="epoch")
    assert resumed.changed is True
    assert resumed.epoch_id == 1
    assert resumed.retired_at is None
    assert resumed.event.event_type == "resumed"

    fixed_clock.advance(60)
    summary = ledger.run([_enabled(source)])
    assert summary.counts.sources_total == 1
    assert summary.counts.items_new == 0
    inventory = store.inventory_for_run(summary.run_id)
    assert inventory[0]["active"] == 3
    assert inventory[0]["retired"] == 0
    assert inventory[0]["lifecycle"] == "active"


def test_resume_new_epoch_rearchives_same_keys_without_touching_history(
        store, mock_server, fixed_clock, source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    ledger.run([source])
    old_items = {
        row["item_key"]: dict(row)
        for row in store.conn.execute(
            "SELECT * FROM items WHERE source_id=? AND epoch_id=1", (source.id,)
        ).fetchall()
    }
    assert len(old_items) == 3

    fixed_clock.advance(60)
    store.retire_source(source.id, fixed_clock.now_iso())
    fixed_clock.advance(60)
    resumed = store.resume_source(source.id, fixed_clock.now_iso(), mode="new-epoch")
    assert resumed.changed is True
    assert resumed.epoch_id == 2
    assert resumed.event.event_type == "new_epoch"
    assert resumed.event.detail == {"previous_epoch": 1}

    fixed_clock.advance(60)
    summary = ledger.run([_enabled(source)])
    # 相同 item_key 在新纪元重新归档，不覆盖旧档
    assert summary.counts.items_new == 3
    rows = store.conn.execute(
        "SELECT epoch_id, item_key, title, content_hash, missing_since FROM items "
        "WHERE source_id=? ORDER BY epoch_id, item_key", (source.id,)
    ).fetchall()
    assert len(rows) == 6
    for row in rows:
        if row["epoch_id"] == 1:
            old = old_items[row["item_key"]]
            assert row["title"] == old["title"]
            assert row["content_hash"] == old["content_hash"]
            assert row["missing_since"] is None
        else:
            assert row["epoch_id"] == 2
            assert row["item_key"] in old_items

    discovered = [e for e in store.get_events(summary.run_id)
                  if e["event_type"] == "discovered"]
    assert len(discovered) == 3
    assert {e["epoch_id"] for e in discovered} == {2}

    inventory = store.inventory_for_run(summary.run_id)
    assert inventory == [{
        "source_id": source.id, "epoch_id": 2, "lifecycle": "active",
        "total": 6, "active": 3, "missing": 0, "retired": 3,
    }]


def test_resume_modes_are_validated(store, mock_server, fixed_clock,
                                    source_factory, default_policy):
    source = source_factory(mock_server)
    _ledger(store, default_policy, fixed_clock).run([source])
    with pytest.raises(SourceLifecycleError):
        store.resume_source(source.id, fixed_clock.now_iso(), mode="bogus")
    # 活跃来源无需恢复；未退役不能开新纪元
    noop = store.resume_source(source.id, fixed_clock.now_iso(), mode="epoch")
    assert noop.changed is False
    with pytest.raises(SourceLifecycleError):
        store.resume_source(source.id, fixed_clock.now_iso(), mode="new-epoch")


# -- run boundary guard ----------------------------------------------------

def test_running_source_run_blocks_transitions(store, mock_server, fixed_clock,
                                               source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    ledger.run([source])
    run_id = store.create_run(fixed_clock.now_iso(), None)
    store.add_source_run(run_id, _source_run_record(source.id, run_id,
                                                    fixed_clock.now_iso()))
    for action in (
        lambda: store.pause_source(source.id, fixed_clock.now_iso()),
        lambda: store.retire_source(source.id, fixed_clock.now_iso()),
        lambda: store.resume_source(source.id, fixed_clock.now_iso()),
    ):
        with pytest.raises(SourceLifecycleError):
            action()
    store.finish_run(run_id, fixed_clock.now_iso(), "ok", RunCounts())
    fixed_clock.advance(60)
    assert store.retire_source(source.id, fixed_clock.now_iso()).changed is True


# -- restart / persistence --------------------------------------------------

def test_lifecycle_state_survives_reopen(tmp_path, mock_server, fixed_clock,
                                         source_factory, default_policy):
    db_path = tmp_path / "archive.sqlite3"
    source = source_factory(mock_server)
    store = Store(db_path)
    _ledger(store, default_policy, fixed_clock).run([source])
    fixed_clock.advance(60)
    store.retire_source(source.id, fixed_clock.now_iso())
    store.close()

    reopened = Store(db_path)
    try:
        blocked = reopened.sync_sources([_enabled(source)])
        assert source.id in blocked
        mock_server.reset()
        fixed_clock.advance(60)
        summary = _ledger(reopened, default_policy, fixed_clock).run([_enabled(source)])
        assert summary.counts.sources_total == 0
        assert mock_server.snapshot() == []
        inventory = reopened.inventory_for_run(summary.run_id)
        assert inventory[0]["lifecycle"] == "retired"
        assert inventory[0]["retired"] == 3
        assert [e["event_type"] for e in reopened.get_source_events()] == ["retired"]
    finally:
        reopened.close()


# -- reports ---------------------------------------------------------------

def test_reports_distinguish_pause_and_retire(store, mock_server, fixed_clock,
                                              source_factory, default_policy):
    paused = source_factory(mock_server, source_id="paused-src")
    retired = source_factory(mock_server, source_id="retired-src")
    ledger = _ledger(store, default_policy, fixed_clock)
    first = ledger.run([paused, retired])
    fixed_clock.advance(60)
    store.pause_source(paused.id, fixed_clock.now_iso())
    store.retire_source(retired.id, fixed_clock.now_iso())
    fixed_clock.advance(60)
    second = ledger.run([_enabled(paused), _enabled(retired)])

    historical = build_report(store, first.run_id, fixed_clock)
    current = build_report(store, second.run_id, fixed_clock)
    hist_inv = {row["source_id"]: row for row in historical["inventory"]}
    assert hist_inv["paused-src"]["active"] == 3
    assert hist_inv["retired-src"]["retired"] == 0
    cur_inv = {row["source_id"]: row for row in current["inventory"]}
    assert cur_inv["paused-src"]["lifecycle"] == "paused"
    assert cur_inv["paused-src"]["active"] == 3
    assert cur_inv["retired-src"]["lifecycle"] == "retired"
    assert cur_inv["retired-src"]["retired"] == 3

    # 报告完整呈现来源生命周期时间线，区分“最后抓取结果”与“之后人为退役”
    assert [(e["source_id"], e["event_type"]) for e in current["source_events"]] == [
        ("paused-src", "paused"), ("retired-src", "retired"),
    ]
    assert [(e["source_id"], e["event_type"]) for e in historical["source_events"]] == [
        ("paused-src", "paused"), ("retired-src", "retired"),
    ]
    markdown = render_markdown(current).decode("utf-8")
    assert "退役" in markdown
    assert "来源生命周期事件" in markdown


# -- CLI --------------------------------------------------------------------

def test_cli_pause_retire_resume(tmp_path, mock_server, fixed_clock,
                                 source_factory, default_policy, write_feeds,
                                 write_policy, capsys):
    source = source_factory(mock_server)
    feeds_path = write_feeds([source])
    policy_path = write_policy()
    db_path = tmp_path / "data" / "archive.sqlite3"
    fetch_args = ["fetch", "--config", str(feeds_path), "--policy", str(policy_path),
                  "--db", str(db_path), "--quiet"]
    assert main(fetch_args) == 0
    capsys.readouterr()

    assert main(["pause", "--db", str(db_path), "--source", source.id]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["action"] == "pause" and paused["changed"] is True
    assert paused["event"]["event_type"] == "paused"

    # 暂停后配置 enabled=true 也不会触发请求
    mock_server.reset()
    assert main(fetch_args) == 2  # 无启用来源
    assert mock_server.snapshot() == []
    capsys.readouterr()

    assert main(["retire", "--db", str(db_path), "--source", source.id]) == 0
    retired = json.loads(capsys.readouterr().out)
    assert retired["action"] == "retire" and retired["changed"] is True
    assert retired["event"]["detail"]["items_retired"] == 3

    assert main(["retire", "--db", str(db_path), "--source", source.id]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["changed"] is False

    assert main(["resume", "--db", str(db_path), "--source", source.id,
                 "--mode", "new-epoch"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["epoch_id"] == 2
    assert resumed["event"]["event_type"] == "new_epoch"

    assert main(fetch_args) == 0  # 新纪元重新归档
    capsys.readouterr()
    with Store(db_path) as store:
        inventory = store.inventory_for_run(store.latest_run_id())
    assert inventory[0]["epoch_id"] == 2
    assert inventory[0]["active"] == 3
    assert inventory[0]["retired"] == 3


def test_cli_lifecycle_errors(tmp_path, mock_server, source_factory,
                              write_feeds, write_policy, capsys):
    source = source_factory(mock_server)
    feeds_path = write_feeds([source])
    policy_path = write_policy()
    db_path = tmp_path / "data" / "archive.sqlite3"
    assert main(["fetch", "--config", str(feeds_path), "--policy", str(policy_path),
                 "--db", str(db_path), "--quiet"]) == 0
    capsys.readouterr()

    assert main(["retire", "--db", str(db_path), "--source", "unknown"]) == 2
    assert "未知来源" in capsys.readouterr().err
    assert main(["pause", "--db", str(tmp_path / "missing.sqlite3"),
                 "--source", source.id]) == 2
    assert "数据库不存在" in capsys.readouterr().err


# -- schema migration --------------------------------------------------------

V1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    format TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    tags_json TEXT NOT NULL,
    note TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    config_path TEXT,
    sources_total INTEGER NOT NULL DEFAULT 0,
    sources_ok INTEGER NOT NULL DEFAULT 0,
    sources_not_modified INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    items_missing INTEGER NOT NULL DEFAULT 0,
    items_restored INTEGER NOT NULL DEFAULT 0,
    item_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'not_modified', 'failed')),
    http_status INTEGER,
    duration_ms INTEGER NOT NULL,
    body_bytes INTEGER NOT NULL,
    body_sha256 TEXT,
    etag TEXT,
    last_modified TEXT,
    attempts INTEGER NOT NULL,
    items_total INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    items_missing INTEGER NOT NULL DEFAULT 0,
    items_restored INTEGER NOT NULL DEFAULT 0,
    item_errors INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    UNIQUE (run_id, source_id)
);
CREATE TABLE IF NOT EXISTS items (
    source_id TEXT NOT NULL REFERENCES sources(id),
    item_key TEXT NOT NULL,
    raw_id TEXT,
    title TEXT,
    link TEXT,
    summary TEXT,
    content TEXT,
    published_at TEXT,
    authors_json TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_changed_at TEXT,
    missing_since TEXT,
    first_run_id TEXT NOT NULL REFERENCES runs(id),
    last_run_id TEXT NOT NULL REFERENCES runs(id),
    PRIMARY KEY (source_id, item_key)
);
CREATE TABLE IF NOT EXISTS item_enclosures (
    source_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    mime_type TEXT,
    declared_length INTEGER,
    PRIMARY KEY (source_id, item_key, position),
    FOREIGN KEY (source_id, item_key) REFERENCES items(source_id, item_key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    item_key TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('discovered', 'updated', 'missing', 'restored')),
    observed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (source_id, item_key, run_id, event_type)
);
CREATE TABLE IF NOT EXISTS item_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id),
    item_index INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_source_missing
    ON items(source_id, missing_since);
CREATE INDEX IF NOT EXISTS idx_events_run ON item_events(run_id);
CREATE INDEX IF NOT EXISTS idx_issues_run ON item_issues(run_id);
"""


def _seed_v1_database(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')")
    conn.execute(
        "INSERT INTO sources(id,name,url,format,enabled,tags_json,note) "
        "VALUES('legacy','Legacy','http://127.0.0.1:8765/x','auto',1,'[]','')")
    conn.execute(
        "INSERT INTO runs(id,started_at,finished_at,status) "
        "VALUES('run-000001','2024-01-01T00:00:00Z','2024-01-01T00:00:01Z','ok')")
    conn.execute(
        "INSERT INTO source_runs(run_id,source_id,source_name,status,duration_ms,"
        "body_bytes,attempts,observed_at) "
        "VALUES('run-000001','legacy','Legacy','ok',5,10,1,'2024-01-01T00:00:01Z')")
    conn.execute(
        "INSERT INTO items(source_id,item_key,raw_id,title,authors_json,categories_json,"
        "content_hash,first_seen_at,last_seen_at,first_run_id,last_run_id) "
        "VALUES('legacy','id:abc','abc','旧条目','[]','[]','hash1',"
        "'2024-01-01T00:00:01Z','2024-01-01T00:00:01Z','run-000001','run-000001')")
    conn.execute(
        "INSERT INTO item_enclosures(source_id,item_key,position,url,mime_type,"
        "declared_length) VALUES('legacy','id:abc',0,'http://127.0.0.1/f.pdf',"
        "'application/pdf',10)")
    conn.execute(
        "INSERT INTO item_events(source_id,item_key,run_id,event_type,observed_at,"
        "detail_json) VALUES('legacy','id:abc','run-000001','discovered',"
        "'2024-01-01T00:00:01Z','{}')")
    conn.commit()
    conn.close()


def test_migration_from_v1_preserves_data_and_enables_lifecycle(tmp_path, item_factory):
    db_path = tmp_path / "legacy.sqlite3"
    _seed_v1_database(db_path)

    store = Store(db_path)
    try:
        version = store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert version["value"] == "2"
        source = store.conn.execute("SELECT * FROM sources WHERE id='legacy'").fetchone()
        assert source["epoch_id"] == 1
        assert source["paused_at"] is None
        assert source["retired_at"] is None
        item = store.conn.execute(
            "SELECT * FROM items WHERE source_id='legacy'").fetchone()
        assert item["epoch_id"] == 1
        assert item["title"] == "旧条目"
        enclosure = store.conn.execute("SELECT * FROM item_enclosures").fetchone()
        assert enclosure["epoch_id"] == 1
        assert enclosure["url"].endswith("f.pdf")
        event = store.conn.execute("SELECT * FROM item_events").fetchone()
        assert event["epoch_id"] == 1

        inventory = store.inventory_for_run("run-000001")
        assert inventory == [{
            "source_id": "legacy", "epoch_id": 1, "lifecycle": "active",
            "total": 1, "active": 1, "missing": 0, "retired": 0,
        }]

        # 迁移后的库可以完整走退役 -> 新纪元 -> 重新归档同一 item_key
        store.retire_source("legacy", "2024-01-02T00:00:00Z")
        store.resume_source("legacy", "2024-01-03T00:00:00Z", mode="new-epoch")
        run_id = store.create_run("2024-01-04T00:00:00Z", None)
        stats = store.apply_observation(
            "legacy", run_id, "2024-01-04T00:00:00Z", [item_factory()], [])
        assert stats.items_new == 1
        epochs = store.conn.execute(
            "SELECT DISTINCT epoch_id FROM items WHERE source_id='legacy'").fetchall()
        assert sorted(row["epoch_id"] for row in epochs) == [1, 2]
    finally:
        store.close()
