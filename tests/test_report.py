from __future__ import annotations

import json

import pytest

from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.report import ReportError, build_report, render_json, write_reports


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
