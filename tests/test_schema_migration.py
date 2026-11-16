"""Schema migration: a version-1 database must upgrade without losing audit data."""

from __future__ import annotations

import sqlite3

from feedkeep.schema import SCHEMA_VERSION
from feedkeep.store import Store

V1_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
  format TEXT NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  tags_json TEXT NOT NULL, note TEXT NOT NULL, etag TEXT, last_modified TEXT,
  last_checked_at TEXT, last_success_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL, config_path TEXT, sources_total INTEGER NOT NULL DEFAULT 0,
  sources_ok INTEGER NOT NULL DEFAULT 0, sources_not_modified INTEGER NOT NULL DEFAULT 0,
  sources_failed INTEGER NOT NULL DEFAULT 0, items_seen INTEGER NOT NULL DEFAULT 0,
  items_new INTEGER NOT NULL DEFAULT 0, items_updated INTEGER NOT NULL DEFAULT 0,
  items_missing INTEGER NOT NULL DEFAULT 0, items_restored INTEGER NOT NULL DEFAULT 0,
  item_errors INTEGER NOT NULL DEFAULT 0);
CREATE TABLE source_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  source_id TEXT NOT NULL, source_name TEXT NOT NULL, status TEXT NOT NULL,
  http_status INTEGER, duration_ms INTEGER NOT NULL, body_bytes INTEGER NOT NULL,
  body_sha256 TEXT, etag TEXT, last_modified TEXT, attempts INTEGER NOT NULL,
  items_total INTEGER NOT NULL DEFAULT 0, items_new INTEGER NOT NULL DEFAULT 0,
  items_updated INTEGER NOT NULL DEFAULT 0, items_missing INTEGER NOT NULL DEFAULT 0,
  items_restored INTEGER NOT NULL DEFAULT 0, item_errors INTEGER NOT NULL DEFAULT 0,
  error_code TEXT, error_message TEXT, truncated INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT NOT NULL, UNIQUE(run_id, source_id));
CREATE TABLE items (source_id TEXT NOT NULL, item_key TEXT NOT NULL, raw_id TEXT,
  title TEXT, link TEXT, summary TEXT, content TEXT, published_at TEXT,
  authors_json TEXT NOT NULL, categories_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_changed_at TEXT,
  missing_since TEXT, first_run_id TEXT NOT NULL, last_run_id TEXT NOT NULL,
  PRIMARY KEY(source_id, item_key));
CREATE TABLE item_enclosures (source_id TEXT NOT NULL, item_key TEXT NOT NULL,
  position INTEGER NOT NULL, url TEXT NOT NULL, mime_type TEXT, declared_length INTEGER,
  PRIMARY KEY(source_id, item_key, position));
CREATE TABLE item_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
  item_key TEXT NOT NULL, run_id TEXT NOT NULL, event_type TEXT NOT NULL
  CHECK (event_type IN ('discovered','updated','missing','restored')),
  observed_at TEXT NOT NULL, detail_json TEXT NOT NULL,
  UNIQUE(source_id, item_key, run_id, event_type));
CREATE TABLE item_issues (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  source_id TEXT NOT NULL, item_index INTEGER NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL);
"""


def _build_v1_database(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO schema_meta VALUES('schema_version','1')")
    conn.execute(
        "INSERT INTO sources VALUES('s','n','http://127.0.0.1/f','atom',1,'[]','',"
        "NULL,NULL,NULL,NULL,0)"
    )
    conn.execute(
        "INSERT INTO runs VALUES('run-000001','t',NULL,'running',NULL,0,0,0,0,0,0,0,0,0,0)"
    )
    conn.execute(
        "INSERT INTO items VALUES('s','id:abc','urn:x','t',NULL,NULL,NULL,NULL,'[]','[]',"
        "'hash1','2024-01-01T00:00:00Z','2024-01-01T00:00:00Z',NULL,NULL,"
        "'run-000001','run-000001')"
    )
    conn.execute(
        "INSERT INTO item_events(source_id,item_key,run_id,event_type,observed_at,detail_json)"
        " VALUES('s','id:abc','run-000001','discovered','2024-01-01T00:00:00Z','{}')"
    )
    conn.execute(
        "INSERT INTO item_issues(run_id,source_id,item_index,code,message)"
        " VALUES('run-000001','s',1,'ATOM_ITEM_MISSING_ID','x')"
    )
    conn.commit()
    return conn


def test_v1_database_upgrades_and_keeps_audit_rows(tmp_path):
    path = tmp_path / "v1.sqlite3"
    conn = _build_v1_database(path)
    conn.close()

    store = Store(path)
    try:
        version = store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert int(version) == SCHEMA_VERSION
        item_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(items)")}
        assert {"retracted_at", "updated_at"} <= item_cols
        run_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(runs)")}
        assert "items_retracted" in run_cols
        source_cols = {r[1] for r in store.conn.execute("PRAGMA table_info(source_runs)")}
        assert "items_retracted" in source_cols
        # Old event and issue rows survive the item_events rebuild.
        events = store.conn.execute(
            "SELECT event_type FROM item_events ORDER BY id"
        ).fetchall()
        assert [r[0] for r in events] == ["discovered"]
        issues = store.conn.execute("SELECT code FROM item_issues").fetchall()
        assert [r[0] for r in issues] == ["ATOM_ITEM_MISSING_ID"]
        # The widened CHECK constraint now accepts the retracted event type.
        store.conn.execute(
            "INSERT INTO item_events(source_id,item_key,run_id,event_type,"
            "observed_at,detail_json) VALUES('s','id:abc','run-000001',"
            "'retracted','2024-02-01T00:00:00Z','{}')"
        )
        store.conn.commit()
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "v1.sqlite3"
    _build_v1_database(path).close()
    Store(path).close()
    Store(path).close()
    conn = sqlite3.connect(path)
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(version) == SCHEMA_VERSION
    conn.close()


def test_newer_schema_database_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO schema_meta VALUES('schema_version','99')")
    conn.commit()
    conn.close()
    try:
        Store(path)
    except RuntimeError as exc:
        assert "newer than supported" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for a future schema version")
