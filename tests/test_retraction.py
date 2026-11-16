"""Atom tombstone lifecycle: parser -> identity -> Store transaction -> report."""

from __future__ import annotations

from feedkeep.identity import identify_tombstone, normalize_item
from feedkeep.models import (RETENTION_ERASE, RETENTION_RETAIN, FeedSource,
                             NormalizedTombstone)
from feedkeep.parsers.atom import parse_atom
from feedkeep.report import build_report

ATOM_NS = 'xmlns="http://www.w3.org/2005/Atom"'
TOMB_NS = 'xmlns:at="http://purl.org/atompub/tombstones/1.0"'


def _entry(item_id: str, updated: str, *, title: str = "标题",
           content: str = "正文", summary: str = "摘要",
           author: str = "编辑", category: str = "公告",
           enclosure: str | None = "http://127.0.0.1:8765/files/a.pdf") -> str:
    parts = [
        f"<entry><id>{item_id}</id><title>{title}</title>",
        f"<updated>{updated}</updated>",
        '<link rel="alternate" href="http://127.0.0.1:8765/articles/x" />',
        f"<summary>{summary}</summary><content>{content}</content>",
        f"<author><name>{author}</name></author>",
        f'<category term="{category}" />',
    ]
    if enclosure:
        parts.append(
            f'<link rel="enclosure" href="{enclosure}" type="application/pdf" length="10" />'
        )
    parts.append("</entry>")
    return "".join(parts)


def _tombstone(ref: str, when: str) -> str:
    return f'<at:deleted-entry ref="{ref}" when="{when}" />'


def _feed(*parts: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<feed {ATOM_NS} {TOMB_NS}>"
        "<title>t</title><id>urn:f</id><updated>2024-03-01T00:00:00Z</updated>"
        f"{''.join(parts)}</feed>"
    ).encode("utf-8")


def _source(retention_mode: str = RETENTION_RETAIN) -> FeedSource:
    return FeedSource(
        id="s1", name="Source 1", url="http://127.0.0.1:8765/feed.xml",
        format="atom", enabled=True, tags=("test",), note="",
        retention_mode=retention_mode,
    )


def _normalize(body: bytes):
    parsed = parse_atom(body)
    items = [normalize_item(raw) for raw in parsed.items]
    markers = [
        NormalizedTombstone(
            item_key=identify_tombstone(marker).item_key,
            raw_ref=marker.raw_ref, declared_at=marker.declared_at, index=index,
        )
        for index, marker in enumerate(parsed.tombstones)
    ]
    return items, markers, list(parsed.issues)


def _row(store, source_id: str, item_key: str):
    return store.conn.execute(
        "SELECT * FROM items WHERE source_id=? AND item_key=?",
        (source_id, item_key),
    ).fetchone()


def _enclosure_count(store, source_id: str, item_key: str) -> int:
    return store.conn.execute(
        "SELECT COUNT(*) AS n FROM item_enclosures WHERE source_id=? AND item_key=?",
        (source_id, item_key),
    ).fetchone()["n"]


def test_explicit_retraction_is_distinct_from_missing(store):
    source = _source()
    store.sync_sources([source])
    first_body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, markers, issues = _normalize(first_body)
    run1 = store.create_run("2024-01-01T00:00:00Z", None)
    stats1 = store.apply_observation(
        source.id, run1, "2024-01-01T00:00:00Z", items, issues,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    assert stats1.items_new == 1
    item_key = items[0].item_key

    # Second complete response: only the feed header and a tombstone.
    second_body = _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"))
    items2, markers2, issues2 = _normalize(second_body)
    run2 = store.create_run("2024-02-02T00:00:00Z", None)
    stats2 = store.apply_observation(
        source.id, run2, "2024-02-02T00:00:00Z", items2, issues2,
        tombstones=markers2, retention_mode=source.retention_mode,
    )
    assert stats2.items_retracted == 1
    assert stats2.items_missing == 0
    row = _row(store, source.id, item_key)
    assert row["retracted_at"] == "2024-02-01T00:00:00Z"
    assert row["missing_since"] is None
    events = store.get_events(run2)
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "retracted"
    assert event["detail"]["ref"] == "urn:feedkeep:atom:alpha"
    assert event["detail"]["declared_at"] == "2024-02-01T00:00:00Z"
    assert event["detail"]["last_content_hash"]


def test_ordinary_absence_still_marks_missing(store):
    source = _source()
    store.sync_sources([source])
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, markers, issues = _normalize(body)
    run1 = store.create_run("2024-01-01T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", items, issues)
    run2 = store.create_run("2024-02-02T00:00:00Z", None)
    stats = store.apply_observation(source.id, run2, "2024-02-02T00:00:00Z", [], [])
    assert stats.items_missing == 1 and stats.items_retracted == 0
    events = store.get_events(run2)
    assert [event["event_type"] for event in events] == ["missing"]


def test_missing_item_can_still_be_restored(store):
    source = _source()
    store.sync_sources([source])
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-01-01T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", items, issues)
    run2 = store.create_run("2024-02-02T00:00:00Z", None)
    store.apply_observation(source.id, run2, "2024-02-02T00:00:00Z", [], [])
    run3 = store.create_run("2024-03-01T00:00:00Z", None)
    stats = store.apply_observation(
        source.id, run3, "2024-03-01T00:00:00Z", items, issues,
    )
    assert stats.items_restored == 1 and stats.items_retracted == 0
    assert [e["event_type"] for e in store.get_events(run3)] == ["restored"]


def test_retracted_item_reappears_without_restored_event(store):
    source = _source()
    store.sync_sources([source])
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-01-01T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-01T00:00:00Z", items, issues)
    run2 = store.create_run("2024-02-02T00:00:00Z", None)
    _, markers2, issues2 = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"))
    )
    store.apply_observation(
        source.id, run2, "2024-02-02T00:00:00Z", [], issues2,
        tombstones=markers2, retention_mode=source.retention_mode,
    )
    # Same entry is published again later.
    run3 = store.create_run("2024-03-01T00:00:00Z", None)
    stats = store.apply_observation(
        source.id, run3, "2024-03-01T00:00:00Z", items, issues,
    )
    assert stats.items_restored == 0
    row = _row(store, source.id, items[0].item_key)
    assert row["retracted_at"] is None and row["missing_since"] is None
    assert store.get_events(run3) == []


def test_stale_tombstone_keeps_newer_content_and_records_issue(store):
    source = _source()
    store.sync_sources([source])
    # Newer entry (updated 2024-02-10) already archived.
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-02-10T00:00:00Z",
                        content="新版正文"))
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-02-10T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-02-10T00:00:00Z", items, issues)
    # Old tombstone (when 2024-02-01) arrives afterwards.
    run2 = store.create_run("2024-02-11T00:00:00Z", None)
    _, markers2, issues2 = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"))
    )
    stats = store.apply_observation(
        source.id, run2, "2024-02-11T00:00:00Z", [], issues2,
        tombstones=markers2, retention_mode=source.retention_mode,
    )
    assert stats.items_retracted == 0
    row = _row(store, source.id, items[0].item_key)
    assert row["retracted_at"] is None
    assert row["content"] == "新版正文"
    # The stale marker is refused, so the unseen entry follows the ordinary
    # absence rule rather than being treated as a publisher retraction.
    assert [e["event_type"] for e in store.get_events(run2)] == ["missing"]
    stored_issues = store.get_item_issues(run2)
    assert [issue["code"] for issue in stored_issues] == ["TOMBSTONE_STALE"]


def test_same_response_entry_and_tombstone_decided_by_time_not_order(store):
    source = _source()
    store.sync_sources([source])
    # Tombstone placed BEFORE the entry in document order, but entry is newer.
    body = _feed(
        _tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"),
        _entry("urn:feedkeep:atom:alpha", "2024-02-10T00:00:00Z"),
    )
    items, markers, issues = _normalize(body)
    run1 = store.create_run("2024-02-11T00:00:00Z", None)
    stats = store.apply_observation(
        source.id, run1, "2024-02-11T00:00:00Z", items, issues,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    assert stats.items_new == 1 and stats.items_retracted == 0
    row = _row(store, source.id, items[0].item_key)
    assert row["retracted_at"] is None
    assert [issue["code"] for issue in store.get_item_issues(run1)] == [
        "TOMBSTONE_STALE",
    ]
    assert [e["event_type"] for e in store.get_events(run1)] == ["discovered"]


def test_same_response_newer_tombstone_retracts_live_entry(store):
    source = _source()
    store.sync_sources([source])
    # First archive the entry, then a response carrying both with a newer when.
    first = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items0, _, issues0 = _normalize(first)
    run1 = store.create_run("2024-01-03T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-03T00:00:00Z", items0, issues0)
    body = _feed(
        _entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"),
        _tombstone("urn:feedkeep:atom:alpha", "2024-02-05T00:00:00Z"),
    )
    items, markers, issues = _normalize(body)
    run2 = store.create_run("2024-02-06T00:00:00Z", None)
    stats = store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", items, issues,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    assert stats.items_retracted == 1 and stats.items_seen == 0
    events = store.get_events(run2)
    assert [event["event_type"] for event in events] == ["retracted"]
    row = _row(store, source.id, items0[0].item_key)
    assert row["retracted_at"] == "2024-02-05T00:00:00Z"


def test_one_terminal_event_per_item_in_a_run(store):
    source = _source()
    store.sync_sources([source])
    first = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items0, _, issues0 = _normalize(first)
    run1 = store.create_run("2024-01-03T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-03T00:00:00Z", items0, issues0)
    run2 = store.create_run("2024-02-06T00:00:00Z", None)
    _, markers, issues = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-05T00:00:00Z"))
    )
    store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", [], issues,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    events = store.get_events(run2)
    assert len(events) == 1 and events[0]["event_type"] == "retracted"
    # Replaying the same observation writes no duplicate event.
    stats_replay = store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", [], issues,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    assert stats_replay.items_retracted == 0
    assert len(store.get_events(run2)) == 1


def test_retain_mode_keeps_body_enclosures_and_hash(store):
    source = _source(RETENTION_RETAIN)
    store.sync_sources([source])
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-01-03T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-03T00:00:00Z", items, issues)
    original_hash = _row(store, source.id, items[0].item_key)["content_hash"]
    run2 = store.create_run("2024-02-06T00:00:00Z", None)
    _, markers, issues2 = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-05T00:00:00Z"))
    )
    store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", [], issues2,
        tombstones=markers, retention_mode=RETENTION_RETAIN,
    )
    row = _row(store, source.id, items[0].item_key)
    assert row["content"] == "正文"
    assert row["summary"] == "摘要"
    assert row["content_hash"] == original_hash
    assert _enclosure_count(store, source.id, items[0].item_key) == 1
    assert row["retracted_at"] == "2024-02-05T00:00:00Z"


def test_erase_mode_clears_content_but_keeps_key_and_hash(store):
    source = _source(RETENTION_ERASE)
    store.sync_sources([source])
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-01-03T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-03T00:00:00Z", items, issues)
    original_hash = _row(store, source.id, items[0].item_key)["content_hash"]
    run2 = store.create_run("2024-02-06T00:00:00Z", None)
    _, markers, issues2 = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-05T00:00:00Z"))
    )
    store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", [], issues2,
        tombstones=markers, retention_mode=RETENTION_ERASE,
    )
    row = _row(store, source.id, items[0].item_key)
    # Identity and fingerprints remain ...
    assert row["item_key"] == items[0].item_key
    assert row["raw_id"] == "urn:feedkeep:atom:alpha"
    assert row["content_hash"] == original_hash
    # ... but the publisher content and attachments are gone.
    assert row["content"] is None
    assert row["summary"] is None
    assert row["title"] is None
    assert row["link"] is None
    assert row["published_at"] is None
    assert row["authors_json"] == "[]"
    assert row["categories_json"] == "[]"
    assert _enclosure_count(store, source.id, items[0].item_key) == 0
    assert row["retracted_at"] == "2024-02-05T00:00:00Z"
    events = store.get_events(run2)
    assert events[0]["event_type"] == "retracted"
    assert events[0]["detail"]["last_content_hash"] == original_hash


def test_truncated_response_still_processes_tombstone(store):
    source = _source()
    store.sync_sources([source])
    body = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-01-03T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-03T00:00:00Z", items, issues)
    # Truncated response with no live entries: ordinary missing must not fire,
    # but the explicit tombstone is still applied.
    run2 = store.create_run("2024-02-06T00:00:00Z", None)
    _, markers, issues2 = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-05T00:00:00Z"))
    )
    stats = store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", [], issues2, truncated=True,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    assert stats.items_missing == 0
    assert stats.items_retracted == 1
    assert _row(store, source.id, items[0].item_key)["retracted_at"] is not None


def test_unknown_ref_tombstone_changes_nothing(store):
    source = _source()
    store.sync_sources([source])
    run1 = store.create_run("2024-02-06T00:00:00Z", None)
    _, markers, issues = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:ghost", "2024-02-05T00:00:00Z"))
    )
    stats = store.apply_observation(
        source.id, run1, "2024-02-06T00:00:00Z", [], issues,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    assert stats.items_retracted == 0 and stats.items_missing == 0
    assert store.get_events(run1) == []
    assert store.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


def test_report_carries_retraction_counts_and_inventory(store):
    source = _source()
    store.sync_sources([source])
    body = _feed(
        _entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"),
        _entry("urn:feedkeep:atom:beta", "2024-01-03T10:30:00Z", title="乙"),
    )
    items, _, issues = _normalize(body)
    run1 = store.create_run("2024-01-03T00:00:00Z", None)
    store.apply_observation(source.id, run1, "2024-01-03T00:00:00Z", items, issues)
    store.add_source_run(run1, _source_run_record(source.id, run1))
    store.finish_run(run1, "2024-01-03T00:01:00Z", "ok", _counts(items_new=2))

    run2 = store.create_run("2024-02-06T00:00:00Z", None)
    _, markers, issues2 = _normalize(
        _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-05T00:00:00Z"))
    )
    stats = store.apply_observation(
        source.id, run2, "2024-02-06T00:00:00Z", [], issues2,
        tombstones=markers, retention_mode=source.retention_mode,
    )
    record = _source_run_record(source.id, run2, items_retracted=stats.items_retracted)
    store.add_source_run(run2, record)
    counts = _counts(items_retracted=stats.items_retracted)
    store.finish_run(run2, "2024-02-06T00:01:00Z", "ok", counts)

    report = build_report(store, run2)
    assert report["schema_version"] == 2
    assert report["run"]["items_retracted"] == 1
    assert report["sources"][0]["items_retracted"] == 1
    inventory = report["inventory"][0]
    assert inventory["total"] == 2
    # beta was absent from a complete observation, so it is ordinarily missing;
    # alpha is explicitly retracted. The two states stay distinct.
    assert inventory["active"] == 0
    assert inventory["missing"] == 1
    assert inventory["retracted"] == 1
    event_types = {e["event_type"] for e in report["events"]}
    assert event_types == {"missing", "retracted"}
    event = next(e for e in report["events"] if e["event_type"] == "retracted")
    assert event["detail"]["ref"] == "urn:feedkeep:atom:alpha"


def _counts(**overrides):
    from feedkeep.models import RunCounts
    counts = RunCounts(sources_total=1, sources_ok=1)
    for key, value in overrides.items():
        setattr(counts, key, value)
    return counts


def _source_run_record(source_id, run_id, **overrides):
    from feedkeep.models import SourceRunRecord
    values = dict(
        source_id=source_id, source_name="Source 1", status="ok", http_status=200,
        duration_ms=1, body_bytes=10, body_sha256=None, etag=None,
        last_modified=None, attempts=1, items_total=0, items_new=0,
        items_updated=0, items_missing=0, items_restored=0, items_retracted=0,
        item_errors=0, error_code=None, error_message=None, truncated=False,
        observed_at="2024-02-06T00:00:00Z",
    )
    values.update(overrides)
    return SourceRunRecord(**values)
