from __future__ import annotations

import sqlite3

from feedkeep.identity import normalize_item
from feedkeep.lifecycle import plan_observation
from feedkeep.models import ExistingItem, FeedSource, ParsedItem
from feedkeep.parsers.atom import parse_atom


def _source(strategy: str = "source_revision_order") -> FeedSource:
    return FeedSource(
        id="atom-1", name="Atom 1",
        url="http://127.0.0.1:8765/feeds/atom-basic.xml", format="atom",
        enabled=True, tags=(), note="", revision_order=strategy,
    )


def _atom_item(raw_id="urn:feedkeep:item:1", *, published="2024-05-10T10:00:00Z",
               updated, content):
    return normalize_item(ParsedItem(
        raw_id=raw_id, title="稳定标题", link=None, summary=None,
        content=content, published_at=published, updated_at=updated,
    ))


def _existing_from(item, *, revision_updated_at, revision_source="atom_updated",
                   missing_since=None, content_hash=None):
    return ExistingItem(
        source_id="atom-1", item_key=item.item_key, title=item.title,
        link=item.link, summary=item.summary, content=item.content,
        published_at=item.published_at, authors=item.authors,
        categories=item.categories, enclosures=item.enclosures,
        content_hash=content_hash or item.content_hash,
        missing_since=missing_since,
        revision_updated_at=revision_updated_at,
        revision_source=revision_source,
    )


def test_parser_preserves_published_and_updated_separately():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title><id>f</id>
      <entry><id>x</id>
        <published>2024-05-10T10:00:00Z</published>
        <updated>2024-05-10T12:00:00+02:00</updated><title>t</title>
      </entry></feed>"""
    parsed = parse_atom(body).items[0]
    assert parsed.published_at == "2024-05-10T10:00:00Z"
    assert parsed.updated_at == "2024-05-10T10:00:00Z"
    normalized = normalize_item(parsed)
    assert normalized.updated_at == "2024-05-10T10:00:00Z"


def test_planner_rejects_older_revision_as_stale():
    current = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    stale = _atom_item(updated="2024-05-10T11:00:00Z", content="V2")
    existing = _existing_from(current, revision_updated_at="2024-05-10T12:00:00Z")
    plan = plan_observation(existing, stale, "source_revision_order")
    assert not plan.accept_content
    assert not plan.is_updated
    assert [event.event_type for event in plan.events] == ["stale_revision"]
    detail = plan.events[0].detail
    assert detail["content_hash"] == stale.content_hash
    assert detail["current_hash"] == current.content_hash
    assert detail["incoming_updated_at"] == "2024-05-10T11:00:00Z"
    assert detail["watermark_updated_at"] == "2024-05-10T12:00:00Z"
    assert detail["reason"] == "incoming_updated_before_revision_watermark"


def test_planner_accepts_newer_revision():
    current = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    newer = _atom_item(updated="2024-05-11T09:00:00Z", content="V4")
    existing = _existing_from(current, revision_updated_at="2024-05-10T12:00:00Z")
    plan = plan_observation(existing, newer, "source_revision_order")
    assert plan.accept_content
    assert plan.is_updated
    assert [event.event_type for event in plan.events] == ["updated"]


def test_planner_same_stamp_same_hash_is_idempotent():
    current = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    existing = _existing_from(current, revision_updated_at="2024-05-10T12:00:00Z")
    plan = plan_observation(existing, current, "source_revision_order")
    assert plan.accept_content
    assert not plan.is_updated
    assert plan.events == ()


def test_planner_same_stamp_different_hash_is_conflict():
    current = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    divergent = _atom_item(updated="2024-05-10T12:00:00Z", content="V-OTHER")
    existing = _existing_from(current, revision_updated_at="2024-05-10T12:00:00Z")
    plan = plan_observation(existing, divergent, "source_revision_order")
    assert not plan.accept_content
    assert not plan.is_updated
    assert [event.event_type for event in plan.events] == ["revision_conflict"]
    detail = plan.events[0].detail
    assert detail["content_hash"] == divergent.content_hash
    assert detail["current_hash"] == current.content_hash
    assert detail["reason"] == "same_updated_at_different_content"


def test_planner_legacy_row_establishes_baseline_without_stale_event():
    # Pre-upgrade row: NULL watermark means the old revision order is unknown.
    current = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    older = _atom_item(updated="2024-05-09T00:00:00Z", content="V2")
    existing = _existing_from(current, revision_updated_at=None, revision_source=None)
    plan = plan_observation(existing, older, "source_revision_order")
    assert plan.accept_content
    assert plan.revision_baseline_established
    assert all(event.event_type not in ("stale_revision", "revision_conflict")
               for event in plan.events)


def test_planner_without_atom_stamp_falls_back_to_observed_order():
    rss_like = normalize_item(ParsedItem(
        raw_id="g", title="r", link=None, summary=None, content=None,
        published_at="2024-01-01T00:00:00Z", updated_at=None,
    ))
    existing = ExistingItem(
        source_id="s", item_key=rss_like.item_key, title="old", link=None,
        summary=None, content=None, published_at=rss_like.published_at,
        authors=(), categories=(), enclosures=(), content_hash="old",
        missing_since=None, revision_updated_at=None, revision_source=None,
    )
    plan = plan_observation(existing, rss_like, "source_revision_order")
    assert plan.accept_content


def test_observed_order_accepts_rollback():
    current = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    stale = _atom_item(updated="2024-05-10T11:00:00Z", content="V2")
    existing = _existing_from(current, revision_updated_at="2024-05-10T12:00:00Z")
    plan = plan_observation(existing, stale, "observed_order")
    assert plan.accept_content
    assert plan.is_updated
    assert [event.event_type for event in plan.events] == ["updated"]


def test_store_observed_mode_rolls_back_current_projection(store):
    source = _source("observed_order")
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    v3 = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [v3], [])
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    v2 = _atom_item(updated="2024-05-10T11:00:00Z", content="V2")
    stats = store.apply_observation(source.id, run2, "2024-06-02T00:00:00Z", [v2], [])
    row = store.conn.execute(
        "SELECT * FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()
    assert row["content"] == "V2"
    assert stats.items_updated == 1
    assert [e["event_type"] for e in store.get_events(run2)] == ["updated"]


def test_store_revision_mode_keeps_current_and_records_stale(store):
    source = _source("source_revision_order")
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    v3 = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [v3], [],
                            revision_order=source.revision_order)
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    v2 = _atom_item(updated="2024-05-10T11:00:00Z", content="V2")
    stats = store.apply_observation(source.id, run2, "2024-06-02T00:00:00Z", [v2], [],
                                    revision_order=source.revision_order)
    row = store.conn.execute(
        "SELECT * FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()
    assert row["content"] == "V3"
    assert row["content_hash"] == v3.content_hash
    assert row["last_seen_at"] == "2024-06-02T00:00:00Z"
    assert row["last_run_id"] == run2
    assert row["missing_since"] is None
    assert row["revision_updated_at"] == "2024-05-10T12:00:00Z"
    assert (stats.items_seen, stats.items_stale, stats.items_updated,
            stats.items_missing) == (1, 1, 0, 0)
    events = store.get_events(run2)
    assert [e["event_type"] for e in events] == ["stale_revision"]
    assert events[0]["detail"]["source_run_id"] == run2
    assert events[0]["detail"]["content_hash"] == v2.content_hash
    assert events[0]["detail"]["current_hash"] == v3.content_hash


def test_store_revision_mode_accepts_later_revision(store):
    source = _source("source_revision_order")
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    v3 = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [v3], [],
                            revision_order=source.revision_order)
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    v4 = _atom_item(updated="2024-05-11T09:00:00Z", content="V4")
    stats = store.apply_observation(source.id, run2, "2024-06-02T00:00:00Z", [v4], [],
                                    revision_order=source.revision_order)
    row = store.conn.execute(
        "SELECT content, revision_updated_at FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()
    assert row["content"] == "V4"
    assert row["revision_updated_at"] == "2024-05-11T09:00:00Z"
    assert stats.items_updated == 1


def test_store_revision_mode_conflict_keeps_current(store):
    source = _source("source_revision_order")
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    v3 = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [v3], [],
                            revision_order=source.revision_order)
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    other = _atom_item(updated="2024-05-10T12:00:00Z", content="V-OTHER")
    stats = store.apply_observation(source.id, run2, "2024-06-02T00:00:00Z", [other], [],
                                    revision_order=source.revision_order)
    row = store.conn.execute(
        "SELECT content, content_hash FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()
    assert row["content"] == "V3"
    assert stats.items_conflicted == 1 and stats.items_updated == 0
    assert [e["event_type"] for e in store.get_events(run2)] == ["revision_conflict"]


def test_stale_item_still_seen_so_no_missing(store):
    source = _source("source_revision_order")
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    v3 = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    sibling = _atom_item(raw_id="urn:feedkeep:item:2",
                         updated="2024-05-10T12:00:00Z", content="K")
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [v3, sibling], [],
                            revision_order=source.revision_order)
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    stale = _atom_item(updated="2024-05-10T11:00:00Z", content="V2")
    stats = store.apply_observation(
        source.id, run2, "2024-06-02T00:00:00Z", [stale], [],
        revision_order=source.revision_order)
    # The stale item is seen; the absent sibling is the only missing one.
    assert stats.items_stale == 1
    assert stats.items_missing == 1
    stale_row = store.conn.execute(
        "SELECT missing_since FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()
    assert stale_row["missing_since"] is None


def test_legacy_null_watermark_baseline_is_established_on_first_observation(store):
    source = _source("source_revision_order")
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    v3 = _atom_item(updated="2024-05-10T12:00:00Z", content="V3")
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [v3], [])
    # Simulate a pre-feature row: watermark columns were never populated.
    store.conn.execute(
        "UPDATE items SET revision_updated_at=NULL, revision_source=NULL "
        "WHERE source_id=? AND item_key=?", (source.id, v3.item_key))
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    older = _atom_item(updated="2024-05-09T00:00:00Z", content="V2")
    stats = store.apply_observation(source.id, run2, "2024-06-02T00:00:00Z", [older], [],
                                    revision_order=source.revision_order)
    row = store.conn.execute(
        "SELECT * FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()
    assert row["revision_updated_at"] == "2024-05-09T00:00:00Z"
    assert row["revision_source"] == "atom_updated"
    assert stats.items_stale == 0 and stats.items_conflicted == 0
    assert all(e["event_type"] not in ("stale_revision", "revision_conflict")
               for e in store.get_events(run2))
    # Gating is active only after the baseline exists.
    run3 = store.create_run("2024-06-03T00:00:00Z", None)
    ancient = _atom_item(updated="2024-05-08T00:00:00Z", content="V1")
    stats3 = store.apply_observation(source.id, run3, "2024-06-03T00:00:00Z", [ancient], [],
                                     revision_order=source.revision_order)
    assert stats3.items_stale == 1
    assert store.conn.execute(
        "SELECT content FROM items WHERE source_id=? AND item_key=?",
        (source.id, v3.item_key)).fetchone()["content"] == "V2"


def test_rss_items_keep_observed_semantics_under_revision_strategy(store):
    source = FeedSource(
        id="rss-1", name="RSS", url="http://127.0.0.1:8765/feeds/rss-basic.xml",
        format="rss", enabled=True, tags=(), note="",
        revision_order="source_revision_order",
    )
    store.sync_sources([source])
    run1 = store.create_run("2024-06-01T00:00:00Z", None)
    first = normalize_item(ParsedItem(
        raw_id="g", title="r", link=None, summary=None, content=None,
        published_at="2024-01-01T00:00:00Z"))
    store.apply_observation(source.id, run1, "2024-06-01T00:00:00Z", [first], [],
                            revision_order=source.revision_order)
    run2 = store.create_run("2024-06-02T00:00:00Z", None)
    second = normalize_item(ParsedItem(
        raw_id="g", title="r2", link=None, summary=None, content=None,
        published_at="2024-01-01T00:00:00Z"))
    stats = store.apply_observation(source.id, run2, "2024-06-02T00:00:00Z", [second], [],
                                    revision_order=source.revision_order)
    row = store.conn.execute(
        "SELECT title, revision_updated_at FROM items WHERE source_id=? AND item_key=?",
        (source.id, first.item_key)).fetchone()
    assert row["title"] == "r2"
    assert row["revision_updated_at"] is None
    assert stats.items_updated == 1 and stats.items_stale == 0


def test_v1_database_migrates_and_keeps_rows_and_events(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES('schema_version','1');
        CREATE TABLE sources(id TEXT PRIMARY KEY,name TEXT,url TEXT,format TEXT,
            enabled INTEGER,tags_json TEXT,note TEXT,etag TEXT,last_modified TEXT,
            last_checked_at TEXT,last_success_at TEXT,consecutive_failures INTEGER DEFAULT 0);
        CREATE TABLE runs(id TEXT PRIMARY KEY,started_at TEXT,finished_at TEXT,status TEXT,
            config_path TEXT,sources_total INTEGER DEFAULT 0,sources_ok INTEGER DEFAULT 0,
            sources_not_modified INTEGER DEFAULT 0,sources_failed INTEGER DEFAULT 0,
            items_seen INTEGER DEFAULT 0,items_new INTEGER DEFAULT 0,
            items_updated INTEGER DEFAULT 0,items_missing INTEGER DEFAULT 0,
            items_restored INTEGER DEFAULT 0,item_errors INTEGER DEFAULT 0);
        CREATE TABLE source_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,
            source_id TEXT,source_name TEXT,status TEXT,http_status INTEGER,
            duration_ms INTEGER,body_bytes INTEGER,body_sha256 TEXT,etag TEXT,
            last_modified TEXT,attempts INTEGER,items_total INTEGER DEFAULT 0,
            items_new INTEGER DEFAULT 0,items_updated INTEGER DEFAULT 0,
            items_missing INTEGER DEFAULT 0,items_restored INTEGER DEFAULT 0,
            item_errors INTEGER DEFAULT 0,error_code TEXT,error_message TEXT,
            truncated INTEGER DEFAULT 0,observed_at TEXT);
        CREATE TABLE items(source_id TEXT,item_key TEXT,raw_id TEXT,title TEXT,link TEXT,
            summary TEXT,content TEXT,published_at TEXT,authors_json TEXT,
            categories_json TEXT,content_hash TEXT,first_seen_at TEXT,last_seen_at TEXT,
            last_changed_at TEXT,missing_since TEXT,first_run_id TEXT,last_run_id TEXT,
            PRIMARY KEY(source_id,item_key));
        CREATE TABLE item_enclosures(source_id TEXT,item_key TEXT,position INTEGER,
            url TEXT,mime_type TEXT,declared_length INTEGER,
            PRIMARY KEY(source_id,item_key,position));
        CREATE TABLE item_events(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,
            item_key TEXT,run_id TEXT,
            event_type TEXT CHECK(event_type IN ('discovered','updated','missing','restored')),
            observed_at TEXT,detail_json TEXT,
            UNIQUE(source_id,item_key,run_id,event_type));
        CREATE TABLE item_issues(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,
            source_id TEXT,item_index INTEGER,code TEXT,message TEXT);
        INSERT INTO sources VALUES('s1','S','u','atom',1,'[]','',NULL,NULL,NULL,NULL,0);
        INSERT INTO runs VALUES('run-000001','t',NULL,'running',NULL,0,0,0,0,1,1,0,0,0,0);
        INSERT INTO items VALUES('s1','k',NULL,'legacy',NULL,NULL,'body',NULL,'[]','[]',
            'hash','t','t',NULL,NULL,'run-000001','run-000001');
        INSERT INTO item_events(source_id,item_key,run_id,event_type,observed_at,detail_json)
            VALUES('s1','k','run-000001','discovered','t','{}');
        """
    )
    connection.commit()
    connection.close()

    from feedkeep.store import Store

    with Store(db_path) as store:
        assert store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        row = store.conn.execute(
            "SELECT * FROM items WHERE source_id='s1'").fetchone()
        assert row["title"] == "legacy" and row["content_hash"] == "hash"
        assert row["revision_updated_at"] is None and row["revision_source"] is None
        event = store.conn.execute("SELECT event_type FROM item_events").fetchone()
        assert event["event_type"] == "discovered"
        store.conn.execute(
            "INSERT INTO item_events(source_id,item_key,run_id,event_type,"
            "observed_at,detail_json) VALUES('s1','k','run-000001',"
            "'stale_revision','t','{}')")
