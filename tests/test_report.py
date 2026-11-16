from __future__ import annotations

import json

import pytest

from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.models import (NormalizedTombstone, RunCounts, SourceRunRecord)
from feedkeep.report import (ReportError, build_report, render_json,
                             render_markdown, write_reports)


def _run(store, server, clock, policy, source_factory):
    sources = [
        source_factory(server, source_id="z-rss"),
        source_factory(server, path="/feeds/atom-mixed.xml", source_id="a-atom", fmt="atom"),
    ]
    Ledger(store, policy, Fetcher(policy, clock), clock).run(sources, "feeds.yaml")


def test_report_is_stable_and_sorted(store, mock_server, fixed_clock,
                                     default_policy, source_factory, tmp_path):
    _run(store, mock_server, fixed_clock, default_policy, source_factory)
    first_json, first_md, report = write_reports(store, tmp_path / "reports", clock=fixed_clock)
    first_json_bytes = first_json.read_bytes()
    first_md_bytes = first_md.read_bytes()
    second_json, second_md, _ = write_reports(store, tmp_path / "reports", clock=fixed_clock)
    assert second_json.read_bytes() == first_json_bytes
    assert second_md.read_bytes() == first_md_bytes

    parsed = json.loads(first_json_bytes)
    assert parsed["run"]["run_id"] == report["run"]["run_id"]
    assert [source["source_id"] for source in parsed["sources"]] == ["a-atom", "z-rss"]
    assert len(parsed["events"]) >= 5
    assert parsed["item_issues"]
    assert first_json.name == f"fetch-report-{report['run']['run_id']}.json"
    assert first_md.name == f"fetch-report-{report['run']['run_id']}.md"
    assert b"## \xe6\x9d\xa5\xe6\xba\x90\xe6\x98\x8e\xe7\xbb\x86" in first_md_bytes


def test_json_render_has_newline_and_sorted_keys(store, mock_server, fixed_clock,
                                                 default_policy, source_factory):
    _run(store, mock_server, fixed_clock, default_policy, source_factory)
    report = build_report(store, clock=fixed_clock)
    rendered = render_json(report)
    assert rendered.endswith(b"\n")
    assert b'"generated_at"' in rendered
    assert rendered.index(b'"events"') < rendered.index(b'"generated_at"')


def test_no_run_and_unknown_run_raise(store, fixed_clock):
    with pytest.raises(ReportError):
        build_report(store, clock=fixed_clock)
    with pytest.raises(ReportError):
        build_report(store, "run-999999", clock=fixed_clock)


def test_report_uses_explicit_run_id(store, mock_server, fixed_clock,
                                     default_policy, source_factory):
    source = source_factory(mock_server, source_id="a")
    ledger = Ledger(store, default_policy, Fetcher(default_policy, fixed_clock), fixed_clock)
    first = ledger.run([source])
    second = ledger.run([source])
    assert first.run_id != second.run_id
    report = build_report(store, first.run_id, fixed_clock)
    assert report["run"]["run_id"] == first.run_id
    assert report["run"]["items_new"] == 3
    latest = build_report(store, clock=fixed_clock)
    assert latest["run"]["run_id"] == second.run_id


def test_report_documents_retraction_separately_from_missing(
        store, item_factory, fixed_clock):
    from feedkeep.models import FeedSource
    source_id = "s1"
    store.sync_sources([FeedSource(
        id=source_id, name="S", url="http://127.0.0.1:8765/f", format="atom",
        enabled=True, tags=(), note="",
    )])
    item = item_factory(raw_id="urn:feedkeep:atom:alpha")
    run1 = store.create_run("2024-01-01T00:00:00Z", None)
    store.apply_observation(source_id, run1, "2024-01-01T00:00:00Z", [item], [])
    store.add_source_run(run1, SourceRunRecord(
        source_id=source_id, source_name="S", status="ok", http_status=200,
        duration_ms=1, body_bytes=10, body_sha256=None, etag=None,
        last_modified=None, attempts=1, items_total=1, items_new=1,
        items_updated=0, items_missing=0, items_restored=0, items_retracted=0,
        item_errors=0, error_code=None, error_message=None, truncated=False,
        observed_at="2024-01-01T00:00:00Z"))
    store.finish_run(run1, "2024-01-01T00:01:00Z", "ok",
                     RunCounts(sources_total=1, sources_ok=1,
                               items_seen=1, items_new=1))

    run2 = store.create_run("2024-02-02T00:00:00Z", None)
    marker = NormalizedTombstone(item_key=item.item_key, raw_ref=item.raw_id,
                                 declared_at="2024-02-01T00:00:00Z", index=0)
    stats = store.apply_observation(
        source_id, run2, "2024-02-02T00:00:00Z", [], [], tombstones=[marker],
    )
    store.add_source_run(run2, SourceRunRecord(
        source_id=source_id, source_name="S", status="ok", http_status=200,
        duration_ms=1, body_bytes=10, body_sha256=None, etag=None,
        last_modified=None, attempts=1, items_total=0, items_new=0,
        items_updated=0, items_missing=0, items_restored=0,
        items_retracted=stats.items_retracted, item_errors=0,
        error_code=None, error_message=None, truncated=False,
        observed_at="2024-02-02T00:00:00Z"))
    store.finish_run(run2, "2024-02-02T00:01:00Z", "ok",
                     RunCounts(sources_total=1, sources_ok=1,
                               items_retracted=stats.items_retracted))

    report = build_report(store, run2, fixed_clock)
    assert report["schema_version"] == 2
    assert report["run"]["items_retracted"] == 1
    assert report["run"]["items_missing"] == 0
    event = report["events"][0]
    assert event["event_type"] == "retracted"
    assert event["detail"]["ref"] == "urn:feedkeep:atom:alpha"
    assert report["inventory"][0]["retracted"] == 1

    markdown = render_markdown(report).decode("utf-8")
    assert "撤回" in markdown
    # Inventory row: total / active / missing / retracted = 1 / 0 / 0 / 1.
    assert "| s1 | 1 | 0 | 0 | 1 |" in markdown
    # JSON stays sorted and newline-terminated.
    rendered = render_json(report)
    assert rendered.endswith(b"\n")
    assert b'"items_retracted"' in rendered
