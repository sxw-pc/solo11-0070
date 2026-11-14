from __future__ import annotations

from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.models import FeedSource
from feedkeep.policy import Policy


def _ledger(store, policy, clock):
    return Ledger(store, policy, Fetcher(policy, clock), clock)


def test_two_source_run_isolates_parse_failure(store, mock_server, fixed_clock,
                                              source_factory, default_policy):
    good = source_factory(mock_server)
    bad = source_factory(mock_server, path="/feeds/rss-malformed.xml",
                         source_id="rss-malformed", fmt="rss")
    ledger = _ledger(store, default_policy, fixed_clock)
    first = ledger.run([good, bad], "feeds.yaml")
    assert first.status == "partial"
    assert first.counts.sources_total == 2
    assert first.counts.sources_ok == 1
    assert first.counts.sources_failed == 1
    assert first.counts.items_new == 3
    bad_record = next(record for record in first.sources if record.source_id == "rss-malformed")
    assert bad_record.status == "failed"
    assert bad_record.http_status == 200
    assert bad_record.error_code == "PARSE_INVALID_XML"
    assert bad_record.body_sha256

    second = ledger.run([good, bad], "feeds.yaml")
    assert second.counts.sources_not_modified == 1
    assert second.counts.sources_failed == 1
    assert second.counts.items_new == 0
    assert second.counts.items_missing == 0


def test_item_errors_do_not_fail_source(store, mock_server, fixed_clock,
                                        source_factory, default_policy):
    source = source_factory(mock_server, path="/feeds/atom-mixed.xml",
                            source_id="atom-mixed", fmt="atom")
    summary = _ledger(store, default_policy, fixed_clock).run([source])
    assert summary.status == "ok"
    assert summary.counts.items_new == 2
    assert summary.counts.item_errors == 3
    assert summary.sources[0].truncated is False
    issues = store.get_item_issues(summary.run_id)
    assert [issue["code"] for issue in issues] == [
        "ATOM_ITEM_MISSING_ID", "ATOM_ITEM_MISSING_TIME", "ATOM_ITEM_BAD_DATE",
    ]


def test_disabled_sources_are_not_fetched(store, mock_server, fixed_clock,
                                          source_factory, default_policy):
    source = source_factory(mock_server, enabled=False)
    summary = _ledger(store, default_policy, fixed_clock).run([source])
    assert summary.status == "failed"
    assert summary.counts.sources_total == 0
    assert mock_server.snapshot() == []


def test_item_cap_truncates_and_flags(store, mock_server, fixed_clock,
                                      source_factory):
    policy = Policy(max_items_per_feed=2, min_interval_ms=100, max_retries=0)
    source = source_factory(mock_server)
    summary = _ledger(store, policy, fixed_clock).run([source])
    assert summary.status == "ok"
    record = summary.sources[0]
    assert record.items_total == 2
    assert record.truncated is True
    assert record.error_code == "ITEMS_TRUNCATED"


def test_policy_violation_is_a_failed_source(store, fixed_clock, default_policy):
    source = FeedSource(
        id="external", name="external", url="http://198.51.100.4/feed.xml",
        format="auto", enabled=True, tags=(), note="must never be contacted",
    )
    summary = _ledger(store, default_policy, fixed_clock).run([source])
    assert summary.status == "failed"
    record = summary.sources[0]
    assert record.error_code == "POLICY_VIOLATION"
    assert record.attempts == 0
    assert record.http_status is None


def test_source_state_is_updated_after_success(store, mock_server, fixed_clock,
                                               source_factory, default_policy):
    source = source_factory(mock_server)
    ledger = _ledger(store, default_policy, fixed_clock)
    ledger.run([source])
    state = store.get_source_state(source.id)
    assert state.etag
    assert state.last_modified
    assert state.last_checked_at is not None
    assert state.consecutive_failures == 0
