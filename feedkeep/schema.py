"""SQLite schema and version bootstrap."""

from __future__ import annotations

import sqlite3

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
    reconcile_mode TEXT NOT NULL DEFAULT 'atomic',
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

CREATE TABLE IF NOT EXISTS observation_checkpoints (
    source_id TEXT NOT NULL REFERENCES sources(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    chunk_index INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('partial', 'complete')),
    reconcile_mode TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    keys_json TEXT NOT NULL,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    items_missing INTEGER NOT NULL DEFAULT 0,
    items_restored INTEGER NOT NULL DEFAULT 0,
    item_errors INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    full_response INTEGER NOT NULL DEFAULT 1,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, run_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_items_source_missing
    ON items(source_id, missing_since);
CREATE INDEX IF NOT EXISTS idx_events_run ON item_events(run_id);
CREATE INDEX IF NOT EXISTS idx_issues_run ON item_issues(run_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_status
    ON observation_checkpoints(status, source_id, run_id);
"""

# Additive migrations for databases created by an older schema version.
_MIGRATIONS: dict[int, list[str]] = {
    1: [
        "ALTER TABLE source_runs ADD COLUMN reconcile_mode TEXT NOT NULL DEFAULT 'atomic'",
    ],
}


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    statements: list[str] = []
    for version in range(from_version, SCHEMA_VERSION):
        statements.extend(_MIGRATIONS.get(version, []))
    if statements:
        conn.executescript(";\n".join(statements) + ";")


def initialize(conn: sqlite3.Connection) -> None:
    """Create tables once and reject databases from a newer schema."""

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    # ``CREATE TABLE IF NOT EXISTS`` makes this safe on both new databases and
    # older ones that still need additive migrations.
    conn.executescript(SCHEMA_SQL)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    existing_version = int(row["value"]) if row is not None else 0
    if existing_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {existing_version} is newer than supported {SCHEMA_VERSION}"
        )
    if existing_version and existing_version < SCHEMA_VERSION:
        _migrate(conn, existing_version)
        # Re-run after ALTERs so any newly introduced tables/indexes are created.
        conn.executescript(SCHEMA_SQL)
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif existing_version < SCHEMA_VERSION:
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
