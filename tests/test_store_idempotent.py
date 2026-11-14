from __future__ import annotations

from feedkeep.models import FeedSource, ItemIssue


def _source():
    return FeedSource(
        id="s1", name="Source 1", url="http://127.0.0.1:8765/feeds/rss-basic.xml",
        format="auto", enabled=True, tags=("test",), note="",
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
