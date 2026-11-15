from __future__ import annotations

from feedkeep.models import FeedSource, ItemIssue
from feedkeep.identity import IDENTITY_LINK_FIRST, normalize_item
from feedkeep.models import ParsedItem
from feedkeep.migration import apply_migration


def _source(identity_contract="id_first"):
    return FeedSource(
        id="s1", name="Source 1", url="http://127.0.0.1:8765/feeds/rss-basic.xml",
        format="auto", enabled=True, tags=("test",), note="",
        identity_contract=identity_contract,
    )


def _run(store, started="2024-01-01T00:00:00Z"):
    return store.create_run(started, None)


def test_same_observation_is_idempotent(store, item_factory):
    source = _source()
    store.sync_sources([source])
    item = item_factory(summary="summary")
    run1 = _run(store)
    first = store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [item], [])
    assert (first.items_seen, first.items_new, first.items_updated) == (1, 1, 0)
    assert [e["event_type"] for e in store.get_events(run1)] == ["discovered"]

    run2 = _run(store, "2024-01-02T00:00:00Z")
    second = store.apply_observation(source.id, run2, "2024-01-02T00:00:00Z", [item], [])
    assert (second.items_seen, second.items_new, second.items_updated) == (1, 0, 0)
    assert second.items_missing == 0
    assert store.get_events(run2) == []
    row = store.conn.execute("SELECT * FROM items WHERE source_id=? AND item_key=?",
                             (source.id, item.item_key)).fetchone()
    assert row["first_seen_at"] == "2024-01-01T00:00:00Z"
    assert row["last_seen_at"] == "2024-01-02T00:00:00Z"
    assert row["missing_since"] is None


def test_update_and_enclosures_are_replaced(store, item_factory, enclosure):
    source = _source()
    store.sync_sources([source])
    run1 = _run(store)
    first_item = item_factory(
        title="old", enclosures=(enclosure(length=1),),
    )
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [first_item], [])
    run2 = _run(store)
    updated_item = item_factory(
        title="new", content="body",
        enclosures=(enclosure(url="http://127.0.0.1:8765/files/b.txt",
                              mime="text/plain", length=2),),
    )
    stats = store.apply_observation(source.id, run2, "2024-01-02T00:00:00Z",
                                    [updated_item], [])
    assert stats.items_updated == 1
    events = store.get_events(run2)
    assert events[0]["event_type"] == "updated"
    assert events[0]["detail"]["changed_fields"] == ["content", "enclosures", "title"]
    rows = store.conn.execute(
        "SELECT * FROM item_enclosures WHERE source_id=? ORDER BY position", (source.id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["url"].endswith("b.txt")


def test_missing_and_restore(store, item_factory):
    source = _source()
    store.sync_sources([source])
    item = item_factory()
    run1 = _run(store)
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [item], [])
    run2 = _run(store)
    missing = store.apply_observation(source.id, run2, "2024-01-02T00:00:00Z", [], [])
    assert missing.items_missing == 1
    assert store.get_events(run2)[0]["event_type"] == "missing"
    run3 = _run(store)
    restored = store.apply_observation(source.id, run3, "2024-01-03T00:00:00Z", [item], [])
    assert restored.items_restored == 1
    assert restored.items_updated == 0
    assert store.get_events(run3)[0]["event_type"] == "restored"


def test_truncated_observation_does_not_mark_missing(store, item_factory):
    source = _source()
    store.sync_sources([source])
    item = item_factory()
    run1 = _run(store)
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [item], [])
    run2 = _run(store)
    truncated = store.apply_observation(
        source.id, run2, "2024-01-02T00:00:00Z", [], [], truncated=True,
    )
    assert truncated.items_missing == 0
    assert store.get_events(run2) == []


def test_item_issues_are_persisted(store, item_factory):
    source = _source()
    store.sync_sources([source])
    run1 = _run(store)
    issues = [ItemIssue(2, "TEST_ISSUE", "bad item"), ItemIssue(3, "OTHER", "worse")]
    stats = store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [], issues)
    assert stats.item_errors == 2
    assert [(row["item_index"], row["code"]) for row in store.get_item_issues(run1)] == [
        (2, "TEST_ISSUE"), (3, "OTHER"),
    ]


def test_source_identity_contract_is_persisted_and_defaults_to_id_first(store, item_factory):
    store.sync_sources([_source()])
    assert store.get_identity_contract("s1") == "id_first"
    assert store.get_configured_contract("s1") == "id_first"
    run = _run(store)
    store.apply_observation("s1", run, "2024-01-01T00:00:00Z", [item_factory()], [])
    # Declaring a new contract stores the desired state but does not rewrite
    # any item keys until the migration tool is explicitly applied.
    store.sync_sources([_source(IDENTITY_LINK_FIRST)])
    assert store.get_configured_contract("s1") == "link_first"
    assert store.get_identity_contract("s1") == "id_first"
    assert store.get_migration_records("s1") == []

    apply_migration(store, {"s1": IDENTITY_LINK_FIRST}, "2024-03-01T00:00:00Z")
    assert store.get_identity_contract("s1") == "link_first"
    assert store.get_configured_contract("s1") == "link_first"


def test_link_first_observation_rotates_id_without_creating_second_item(store):
    source = _source(IDENTITY_LINK_FIRST)
    store.sync_sources([source])
    link = "http://127.0.0.1:8765/stable-article"
    parsed = ParsedItem(raw_id="publisher-id-1", title="文章", link=link,
                        summary=None, content=None,
                        published_at="2024-01-01T00:00:00Z")
    first = normalize_item(parsed, IDENTITY_LINK_FIRST)
    run1 = _run(store)
    stats = store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [first], [])
    assert stats.items_new == 1

    rotated = ParsedItem(raw_id="publisher-id-2", title="文章", link=link,
                         summary=None, content=None,
                         published_at="2024-01-01T00:00:00Z")
    second = normalize_item(rotated, IDENTITY_LINK_FIRST)
    assert second.item_key == first.item_key
    run2 = _run(store, "2024-02-01T00:00:00Z")
    stats = store.apply_observation(source.id, run2, "2024-02-01T00:00:00Z", [second], [])
    assert stats.items_new == 0 and stats.items_missing == 0
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id='s1'"
    ).fetchone()["n"] == 1


def test_migrated_item_keeps_lifecycle_and_event_chain_across_runs(store):
    source = _source()
    store.sync_sources([source])
    parsed = ParsedItem(raw_id="urn:1", title="文章",
                        link="http://127.0.0.1:8765/a", summary=None, content=None,
                        published_at="2024-01-01T00:00:00Z")
    item = normalize_item(parsed, "id_first")
    run1 = _run(store)
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", [item], [])
    run2 = _run(store, "2024-02-01T00:00:00Z")
    store.apply_observation(source.id, run2, "2024-02-01T00:00:00Z", [], [])

    report = apply_migration(store, {"s1": IDENTITY_LINK_FIRST},
                             "2024-03-01T00:00:00Z")
    assert report.items_to_migrate == 1
    new_key = report.plans[0].mappings[0].new_key

    # Event chain order survived the rename, identity_changed sits on new key.
    events = store.conn.execute(
        "SELECT event_type, item_key FROM item_events WHERE source_id='s1' ORDER BY id"
    ).fetchall()
    assert [(row["event_type"], row["item_key"]) for row in events] == [
        ("discovered", new_key),
        ("missing", new_key),
        ("identity_changed", new_key),
    ]
    # Re-running the same migration adds no duplicate identity_changed event.
    apply_migration(store, {"s1": IDENTITY_LINK_FIRST}, "2024-03-02T00:00:00Z")
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM item_events "
        "WHERE source_id='s1' AND event_type='identity_changed'"
    ).fetchone()["n"] == 1

    # A later observation on the new key restores the item through the normal
    # lifecycle path, proving first_seen/missing state remain coherent.
    run3 = _run(store, "2024-04-01T00:00:00Z")
    stats = store.apply_observation(
        source.id, run3, "2024-04-01T00:00:00Z",
        [normalize_item(parsed, IDENTITY_LINK_FIRST)], [],
    )
    assert stats.items_restored == 1 and stats.items_new == 0
    row = store.conn.execute(
        "SELECT first_seen_at, missing_since FROM items WHERE source_id='s1' AND item_key=?",
        (new_key,),
    ).fetchone()
    assert row["first_seen_at"] == "2024-01-01T00:00:00Z"
    assert row["missing_since"] is None
