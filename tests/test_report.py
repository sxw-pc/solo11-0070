from __future__ import annotations

import json

import pytest

from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.report import ReportError, build_report, render_json, render_markdown, write_reports


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


def test_report_surfaces_reconcile_mode_and_coverage(
    store, mock_server, fixed_clock, default_policy, source_factory, item_factory,
):
    from feedkeep.lifecycle import PROGRESSIVE_VISIBLE

    source = source_factory(mock_server)
    ledger = Ledger(
        store, default_policy, Fetcher(default_policy, fixed_clock), fixed_clock,
        reconcile_mode=PROGRESSIVE_VISIBLE, chunk_size=1,
    )
    summary = ledger.run([source])
    report = build_report(store, summary.run_id, fixed_clock)
    assert report["schema_version"] == 2
    assert report["sources"][0]["reconcile_mode"] == PROGRESSIVE_VISIBLE
    assert report["partial_coverage"] == []
    markdown = render_markdown(report).decode("utf-8")
    assert "对账模式" in markdown
    assert "progressive" in markdown
    assert "渐进观察覆盖检查点（未完成）" in markdown
    assert "无。" in markdown


def test_report_lists_partial_progressive_coverage(
    store, fixed_clock, item_factory,
):
    from feedkeep.lifecycle import PROGRESSIVE_VISIBLE
    from feedkeep.models import FeedSource

    source = FeedSource(
        id="partial-src", name="partial", url="http://127.0.0.1:8765/feed.xml",
        format="auto", enabled=True, tags=("test",), note="",
    )
    store.sync_sources([source])
    run = store.create_run("2024-03-01T00:00:00Z", None)
    observed = [item_factory(raw_id=f"item-{index}") for index in range(4)]
    calls = {"n": 0}

    def fail_second(sql):
        if "OBSERVATION_CHECKPOINTS" in sql.upper():
            calls["n"] += 1
            if calls["n"] == 2:
                return True
        return False

    store.fail_writes(fail_second)
    try:
        with pytest.raises(RuntimeError):
            store.apply_observation(
                source.id, run, "2024-03-01T00:00:00Z", observed, [],
                mode=PROGRESSIVE_VISIBLE, chunk_size=2,
            )
    finally:
        store.fail_writes(None)

    # The run is still "running" so build_report would reject it; query coverage
    # through the store seam and exercise the markdown rendering with a finished
    # run built from the same partial state.
    coverage = store.get_observation_coverage(run)
    assert len(coverage) == 1
    entry = coverage[0]
    assert entry["source_id"] == source.id
    assert entry["reconcile_mode"] == PROGRESSIVE_VISIBLE
    assert entry["keys_covered"] == 2
    assert entry["full_response_confirmed"] is False

    fake_report = {
        "schema_version": 2,
        "generated_at": fixed_clock.now_iso(),
        "run": {"run_id": run, "status": "partial", "started_at": "t",
                "finished_at": "t", "config_path": None, "sources_total": 1,
                "sources_ok": 0, "sources_not_modified": 0, "sources_failed": 1,
                "items_seen": 0, "items_new": 0, "items_updated": 0,
                "items_missing": 0, "items_restored": 0, "item_errors": 0},
        "sources": [],
        "item_issues": [],
        "events": [],
        "inventory": [],
        "partial_coverage": coverage,
    }
    markdown = render_markdown(fake_report).decode("utf-8")
    assert "渐进观察覆盖检查点（未完成）" in markdown
    assert "1/2" in markdown
    assert PROGRESSIVE_VISIBLE in markdown
