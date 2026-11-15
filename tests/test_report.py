from __future__ import annotations

import json

import pytest

from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.report import ReportError, build_report, render_json, write_reports


class SimulatedCrash(BaseException):
    pass


class CrashingFetcher:
    def __init__(self, inner, crash_at):
        self.inner = inner
        self.crash_at = crash_at
        self.calls = 0

    def fetch(self, url, etag=None, last_modified=None):
        if self.calls == self.crash_at:
            raise SimulatedCrash("simulated crash")
        self.calls += 1
        return self.inner.fetch(url, etag=etag, last_modified=last_modified)


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


def test_report_rejects_a_still_running_run(store, mock_server, fixed_clock,
                                            default_policy, source_factory):
    sources = [
        source_factory(mock_server, source_id="a"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="b", fmt="jsonfeed"),
    ]
    ledger = Ledger(store, default_policy,
                    CrashingFetcher(Fetcher(default_policy, fixed_clock), 1), fixed_clock)
    with pytest.raises(SimulatedCrash):
        ledger.run(sources, "feeds.yaml")
    run_id = store.running_run()["id"]
    with pytest.raises(ReportError) as excinfo:
        build_report(store, run_id, fixed_clock)
    assert "recover" in str(excinfo.value)


def test_interrupted_report_shows_completed_and_pending_sources(
    store, mock_server, fixed_clock, default_policy, source_factory
):
    sources = [
        source_factory(mock_server, source_id="a-rss"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="b-json", fmt="jsonfeed"),
        source_factory(mock_server, path="/feeds/atom-mixed.xml",
                       source_id="c-atom", fmt="atom"),
    ]
    crashing = Ledger(store, default_policy,
                      CrashingFetcher(Fetcher(default_policy, fixed_clock), 1), fixed_clock)
    with pytest.raises(SimulatedCrash):
        crashing.run(sources, "feeds.yaml")
    run_id = store.running_run()["id"]

    outcome = Ledger(store, default_policy, Fetcher(default_policy, fixed_clock),
                     fixed_clock).finalize(run_id, sources, "feeds.yaml")
    report = build_report(store, run_id, fixed_clock)

    assert report["run"]["status"] == "interrupted"
    assert report["progress"]["interrupted"] is True
    assert report["progress"]["handling"] == "finalize"
    assert report["progress"]["completed_sources"] == ["a-rss"]
    assert report["progress"]["pending_sources"] == ["b-json", "c-atom"]
    assert report["recovery"]["prior_run_id"] is None
    assert report["recovery"]["successor_run_id"] == outcome.run_id
    assert report["run"]["items_new"] == 3
    assert {source["source_id"]: source["processing"]
            for source in report["sources"]} == {"a-rss": "recorded"}

    successor = build_report(store, outcome.run_id, fixed_clock)
    assert successor["run"]["status"] == "ok"
    assert successor["progress"]["handling"] == "finalize"
    assert successor["recovery"]["prior_run_id"] == run_id
    assert successor["progress"]["pending_sources"] == []

    json_bytes = render_json(report)
    assert b'"recovery"' in json_bytes


def test_resumed_report_marks_reused_and_processed_sources(
    store, mock_server, fixed_clock, default_policy, source_factory
):
    sources = [
        source_factory(mock_server, source_id="a-rss"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="b-json", fmt="jsonfeed"),
    ]
    crashing = Ledger(store, default_policy,
                      CrashingFetcher(Fetcher(default_policy, fixed_clock), 1), fixed_clock)
    with pytest.raises(SimulatedCrash):
        crashing.run(sources, "feeds.yaml")
    run_id = store.running_run()["id"]

    outcome = Ledger(store, default_policy, Fetcher(default_policy, fixed_clock),
                     fixed_clock).resume(run_id, sources, "feeds.yaml")
    report = build_report(store, run_id, fixed_clock)

    assert report["run"]["status"] == "ok"
    assert report["progress"]["handling"] == "resume"
    assert report["progress"]["completed_sources"] == ["a-rss", "b-json"]
    assert report["progress"]["pending_sources"] == []
    assert report["recovery"]["reused_sources"] == ["a-rss"]
    assert report["recovery"]["processed_sources"] == ["b-json"]
    processing = {source["source_id"]: source["processing"]
                  for source in report["sources"]}
    assert processing == {"a-rss": "resumed", "b-json": "processed"}
    assert report["run"]["items_new"] == 6
    assert outcome.reused_source_ids == ("a-rss",)
