"""SQLite schema and version bootstrap."""

from __future__ import annotations

import sqlite3
from typing import Optional

SCHEMA_VERSION = 2

SCHEMA_SQL = """
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
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    epoch_id INTEGER NOT NULL DEFAULT 1,
    paused_at TEXT,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS source_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('paused', 'retired', 'resumed', 'new_epoch')),
    epoch_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    last_run_id TEXT REFERENCES runs(id),
    detail_json TEXT NOT NULL
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
    epoch_id INTEGER NOT NULL DEFAULT 1,
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
    PRIMARY KEY (source_id, epoch_id, item_key)
);

CREATE TABLE IF NOT EXISTS item_enclosures (
    source_id TEXT NOT NULL,
    epoch_id INTEGER NOT NULL DEFAULT 1,
    item_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    mime_type TEXT,
    declared_length INTEGER,
    PRIMARY KEY (source_id, epoch_id, item_key, position),
    FOREIGN KEY (source_id, epoch_id, item_key)
        REFERENCES items(source_id, epoch_id, item_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    epoch_id INTEGER NOT NULL DEFAULT 1,
    item_key TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('discovered', 'updated', 'missing', 'restored')),
    observed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (source_id, epoch_id, item_key, run_id, event_type)
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
    ON items(source_id, epoch_id, missing_since);
CREATE INDEX IF NOT EXISTS idx_events_run ON item_events(run_id);
CREATE INDEX IF NOT EXISTS idx_issues_run ON item_issues(run_id);
CREATE INDEX IF NOT EXISTS idx_source_events_source ON source_events(source_id, id);
"""

# v1 -> v2: source lifecycle (pause/retire/resume, epochs). Existing sources and
# items are migrated into epoch 1; item identity becomes (source_id, epoch_id,
# item_key) so a later epoch can re-archive the same item_key without touching
# retired history. Foreign keys must stay disabled while the tables are rebuilt.
MIGRATION_1_TO_2_SQL = """
PRAGMA foreign_keys=OFF;
BEGIN;

ALTER TABLE sources ADD COLUMN epoch_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sources ADD COLUMN paused_at TEXT;
ALTER TABLE sources ADD COLUMN retired_at TEXT;

CREATE TABLE source_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('paused', 'retired', 'resumed', 'new_epoch')),
    epoch_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    last_run_id TEXT REFERENCES runs(id),
    detail_json TEXT NOT NULL
);

ALTER TABLE item_events RENAME TO item_events_old;
CREATE TABLE item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    epoch_id INTEGER NOT NULL DEFAULT 1,
    item_key TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('discovered', 'updated', 'missing', 'restored')),
    observed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (source_id, epoch_id, item_key, run_id, event_type)
);
INSERT INTO item_events(id, source_id, epoch_id, item_key, run_id, event_type,
                        observed_at, detail_json)
SELECT id, source_id, 1, item_key, run_id, event_type, observed_at, detail_json
FROM item_events_old;
DROP TABLE item_events_old;

CREATE TABLE items_new (
    source_id TEXT NOT NULL REFERENCES sources(id),
    epoch_id INTEGER NOT NULL DEFAULT 1,
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
    PRIMARY KEY (source_id, epoch_id, item_key)
);
INSERT INTO items_new(
    source_id, epoch_id, item_key, raw_id, title, link, summary, content,
    published_at, authors_json, categories_json, content_hash, first_seen_at,
    last_seen_at, last_changed_at, missing_since, first_run_id, last_run_id)
SELECT source_id, 1, item_key, raw_id, title, link, summary, content,
       published_at, authors_json, categories_json, content_hash, first_seen_at,
       last_seen_at, last_changed_at, missing_since, first_run_id, last_run_id
FROM items;
DROP TABLE items;
ALTER TABLE items_new RENAME TO items;

CREATE TABLE item_enclosures_new (
    source_id TEXT NOT NULL,
    epoch_id INTEGER NOT NULL DEFAULT 1,
    item_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    mime_type TEXT,
    declared_length INTEGER,
    PRIMARY KEY (source_id, epoch_id, item_key, position),
    FOREIGN KEY (source_id, epoch_id, item_key)
        REFERENCES items(source_id, epoch_id, item_key) ON DELETE CASCADE
);
INSERT INTO item_enclosures_new(
    source_id, epoch_id, item_key, position, url, mime_type, declared_length)
SELECT source_id, 1, item_key, position, url, mime_type, declared_length
FROM item_enclosures;
DROP TABLE item_enclosures;
ALTER TABLE item_enclosures_new RENAME TO item_enclosures;

DROP INDEX IF EXISTS idx_items_source_missing;
CREATE INDEX idx_items_source_missing ON items(source_id, epoch_id, missing_since);
CREATE INDEX idx_events_run ON item_events(run_id);
CREATE INDEX idx_source_events_source ON source_events(source_id, id);

UPDATE schema_meta SET value='2' WHERE key='schema_version';
COMMIT;
PRAGMA foreign_keys=ON;
"""


def _stored_version(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if row is None:
        return None
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else None


def initialize(conn: sqlite3.Connection) -> None:
    """Create or migrate the schema and reject databases from a newer schema."""

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    version = _stored_version(conn)
    if version is None:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {version} is newer than supported {SCHEMA_VERSION}"
        )
    if version < SCHEMA_VERSION:
        # Only one migration step exists so far; keep the chain explicit.
        conn.executescript(MIGRATION_1_TO_2_SQL)
    else:
        # Already current: idempotently ensure every object exists.
        conn.executescript(SCHEMA_SQL)
        conn.commit()
