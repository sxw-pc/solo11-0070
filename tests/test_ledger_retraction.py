"""End-to-end tombstone flow through Ledger using inline Atom bytes (no network)."""

from __future__ import annotations

import hashlib

from feedkeep.ledger import Ledger
from feedkeep.models import RETENTION_ERASE, RETENTION_RETAIN, FetchResult, FeedSource
from feedkeep.policy import Policy
from feedkeep.report import build_report

ATOM_NS = 'xmlns="http://www.w3.org/2005/Atom"'
TOMB_NS = 'xmlns:at="http://purl.org/atompub/tombstones/1.0"'


def _entry(item_id: str, updated: str, **overrides) -> str:
    title = overrides.get("title", "标题")
    content = overrides.get("content", "正文")
    return (
        f"<entry><id>{item_id}</id><title>{title}</title>"
        f"<updated>{updated}</updated>"
        '<link rel="alternate" href="http://127.0.0.1:8765/articles/x" />'
        f"<summary>摘要</summary><content>{content}</content>"
        '<link rel="enclosure" href="http://127.0.0.1:8765/files/a.pdf" '
        'type="application/pdf" length="10" /></entry>'
    )


def _tombstone(ref: str, when: str) -> str:
    return f'<at:deleted-entry ref="{ref}" when="{when}" />'


def _feed(*parts: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><feed {ATOM_NS} {TOMB_NS}>'
        f"<title>t</title><id>urn:f</id><updated>2024-03-01T00:00:00Z</updated>"
        f"{''.join(parts)}</feed>"
    ).encode("utf-8")


class _ScriptedFetcher:
    """Stand-in for Fetcher returning queued feed bodies without sockets."""

    def __init__(self, bodies):
        self._bodies = list(bodies)

    def fetch(self, url, etag=None, last_modified=None):
        body = self._bodies.pop(0)
        return FetchResult(
            url=url, status=200, attempts=1, duration_ms=1,
            body_bytes=len(body),
            body_sha256=hashlib.sha256(body).hexdigest(),
            body=body, etag=None, last_modified=None,
            content_type="application/atom+xml",
        )


def _source(retention_mode: str = RETENTION_RETAIN) -> FeedSource:
    return FeedSource(
        id="atom-1", name="Atom 源", url="http://127.0.0.1:8765/feed.xml",
        format="atom", enabled=True, tags=("test",), note="",
        retention_mode=retention_mode,
    )


def _ledger(store, clock, fetcher):
    return Ledger(store, Policy(), fetcher, clock)


def test_ledger_records_retraction_instead_of_missing(store, fixed_clock):
    first = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    second = _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"))
    fetcher = _ScriptedFetcher([first, second])
    ledger = _ledger(store, fixed_clock, fetcher)

    run1 = ledger.run([_source()], "feeds.yaml")
    assert run1.counts.items_new == 1
    assert run1.counts.items_retracted == 0

    run2 = ledger.run([_source()], "feeds.yaml")
    assert run2.counts.items_missing == 0
    assert run2.counts.items_retracted == 1
    record = run2.sources[0]
    assert record.items_retracted == 1 and record.items_missing == 0

    report = build_report(store, run2.run_id, fixed_clock)
    events = report["events"]
    assert [event["event_type"] for event in events] == ["retracted"]
    assert events[0]["detail"]["ref"] == "urn:feedkeep:atom:alpha"
    assert events[0]["detail"]["declared_at"] == "2024-02-01T00:00:00Z"
    inventory = report["inventory"][0]
    assert inventory["retracted"] == 1
    assert inventory["missing"] == 0 and inventory["active"] == 0


def test_ledger_rejects_stale_tombstone_for_newer_entry(store, fixed_clock):
    first = _feed(_entry("urn:feedkeep:atom:alpha", "2024-02-10T00:00:00Z",
                         content="新版正文"))
    second = _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"))
    fetcher = _ScriptedFetcher([first, second])
    ledger = _ledger(store, fixed_clock, fetcher)

    ledger.run([_source()], "feeds.yaml")
    run2 = ledger.run([_source()], "feeds.yaml")
    # Refused retraction: the entry is treated as an ordinary absence.
    assert run2.counts.items_retracted == 0
    assert run2.counts.items_missing == 1
    issues = store.get_item_issues(run2.run_id)
    assert [issue["code"] for issue in issues] == ["TOMBSTONE_STALE"]
    row = store.conn.execute("SELECT content, retracted_at FROM items").fetchone()
    assert row["content"] == "新版正文" and row["retracted_at"] is None


def test_ledger_erase_policy_clears_content_but_keeps_evidence(store, fixed_clock):
    first = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    second = _feed(_tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"))
    fetcher = _ScriptedFetcher([first, second])
    ledger = _ledger(store, fixed_clock, fetcher)

    ledger.run([_source(RETENTION_ERASE)], "feeds.yaml")
    original_hash = store.conn.execute(
        "SELECT content_hash FROM items"
    ).fetchone()["content_hash"]
    run2 = ledger.run([_source(RETENTION_ERASE)], "feeds.yaml")
    assert run2.counts.items_retracted == 1
    row = store.conn.execute("SELECT * FROM items").fetchone()
    assert row["content"] is None and row["summary"] is None
    assert row["title"] is None and row["authors_json"] == "[]"
    assert row["content_hash"] == original_hash
    assert row["raw_id"] == "urn:feedkeep:atom:alpha"
    enclosure_count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM item_enclosures"
    ).fetchone()["n"]
    assert enclosure_count == 0
    events = store.get_events(run2.run_id)
    assert events[0]["event_type"] == "retracted"
    assert events[0]["detail"]["last_content_hash"] == original_hash


def test_ledger_tombstone_duplicate_ref_is_item_issue(store, fixed_clock):
    first = _feed(_entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"))
    second = _feed(
        _tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"),
        _tombstone("urn:feedkeep:atom:alpha", "2024-02-02T00:00:00Z"),
    )
    fetcher = _ScriptedFetcher([first, second])
    ledger = _ledger(store, fixed_clock, fetcher)
    ledger.run([_source()], "feeds.yaml")
    run2 = ledger.run([_source()], "feeds.yaml")
    assert run2.counts.items_retracted == 1
    issues = store.get_item_issues(run2.run_id)
    assert [issue["code"] for issue in issues] == [
        "ATOM_TOMBSTONE_DUPLICATE_REF",
    ]
    # Only one terminal retracted event exists.
    events = [e for e in store.get_events(run2.run_id)
              if e["event_type"] == "retracted"]
    assert len(events) == 1
    assert events[0]["detail"]["declared_at"] == "2024-02-01T00:00:00Z"


def test_ledger_tombstone_applies_even_when_response_truncated(store, fixed_clock):
    # A policy with a tiny item cap forces the truncation path.
    policy = Policy(max_items_per_feed=1, min_interval_ms=100, max_retries=0)
    first = _feed(
        _entry("urn:feedkeep:atom:alpha", "2024-01-02T08:00:00Z"),
        _entry("urn:feedkeep:atom:beta", "2024-01-03T00:00:00Z", title="乙"),
    )
    # The truncated second response still carries the explicit tombstone.
    truncated_second = _feed(
        _entry("urn:feedkeep:atom:beta", "2024-01-03T00:00:00Z", title="乙"),
        _entry("urn:feedkeep:atom:gamma", "2024-01-04T00:00:00Z", title="丙"),
        _tombstone("urn:feedkeep:atom:alpha", "2024-02-01T00:00:00Z"),
    )
    fetcher = _ScriptedFetcher([first, truncated_second])
    ledger = Ledger(store, policy, fetcher, fixed_clock)
    ledger.run([_source()], "feeds.yaml")
    run2 = ledger.run([_source()], "feeds.yaml")
    assert run2.sources[0].truncated is True
    # No missing reconciliation under truncation ...
    assert run2.counts.items_missing == 0
    # ... but the explicit deletion marker still retracts alpha.
    assert run2.counts.items_retracted == 1
    row = store.conn.execute(
        "SELECT retracted_at FROM items WHERE raw_id=?",
        ("urn:feedkeep:atom:alpha",),
    ).fetchone()
    assert row["retracted_at"] == "2024-02-01T00:00:00Z"
