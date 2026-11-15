from __future__ import annotations

import json

import pytest

from feedkeep.cli import main
from feedkeep.clock import SystemClock
from feedkeep.demo import run_demo
from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.mockdata import check_fixtures
from feedkeep.store import Store


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


def test_cli_fetch_and_report(tmp_path, mock_server, source_factory,
                              write_feeds, write_policy, capsys):
    sources = [
        source_factory(mock_server, source_id="rss"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="jsonfeed", fmt="jsonfeed"),
        source_factory(mock_server, path="/feeds/rss-malformed.xml",
                       source_id="broken", fmt="rss"),
    ]
    feeds_path = write_feeds(sources)
    policy_path = write_policy()
    db_path = tmp_path / "data" / "archive.sqlite3"
    out_dir = tmp_path / "reports"
    fetch_code = main([
        "fetch", "--config", str(feeds_path), "--policy", str(policy_path),
        "--db", str(db_path), "--quiet",
    ])
    assert fetch_code == 1
    assert db_path.exists()
    assert "ok=2" in capsys.readouterr().out

    report_code = main(["report", "--db", str(db_path), "--out", str(out_dir)])
    assert report_code == 0
    json_reports = list(out_dir.glob("fetch-report-*.json"))
    markdown_reports = list(out_dir.glob("fetch-report-*.md"))
    assert len(json_reports) == len(markdown_reports) == 1
    report = json.loads(json_reports[0].read_text(encoding="utf-8"))
    assert report["run"]["status"] == "partial"
    assert len(report["sources"]) == 3
    assert any(source["error_code"] == "PARSE_INVALID_XML" for source in report["sources"])


def test_demo_self_check_and_reports(tmp_path):
    summary = run_demo(out_dir=tmp_path / "demo", port=0)
    assert summary["first_run"]["sources_total"] == 6
    assert summary["first_run"]["sources_failed"] == 1
    assert summary["second_run"]["sources_not_modified"] >= 4
    assert (tmp_path / "demo").exists()
    latest = json.loads(open(summary["json_report"], encoding="utf-8").read())
    assert latest["run"]["run_id"] == summary["second_run"]["run_id"]
    first = json.loads(open(summary["first_json_report"], encoding="utf-8").read())
    assert first["item_issues"]


def test_bundled_fixtures_have_not_drifted():
    assert check_fixtures() == []


def _interrupt_after_first_source(db_path, policy, sources):
    clock = SystemClock()
    with Store(db_path) as store:
        ledger = Ledger(store, policy,
                        CrashingFetcher(Fetcher(policy, clock), 1), clock)
        with pytest.raises(SimulatedCrash):
            ledger.run(sources, "feeds.yaml")
        return store.running_run()["id"]


def test_cli_recover_resume_and_finalize(tmp_path, mock_server, source_factory,
                                         write_feeds, write_policy, capsys):
    from feedkeep.config import load_policy

    sources = [
        source_factory(mock_server, source_id="rss"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="jsonfeed", fmt="jsonfeed"),
    ]
    feeds_path = write_feeds(sources)
    policy_path = write_policy()
    policy = load_policy(policy_path)
    db_path = tmp_path / "data" / "archive.sqlite3"
    out_dir = tmp_path / "reports"

    interrupted_id = _interrupt_after_first_source(db_path, policy, sources)
    capsys.readouterr()

    resume_code = main([
        "recover", "--config", str(feeds_path), "--policy", str(policy_path),
        "--db", str(db_path), "--mode", "resume", "--quiet",
    ])
    assert resume_code == 0
    assert f"{interrupted_id} resume" in capsys.readouterr().out

    report_code = main(["report", "--db", str(db_path), "--out", str(out_dir)])
    assert report_code == 0
    report = json.loads(next(out_dir.glob("fetch-report-*.json")).read_text("utf-8"))
    assert report["run"]["run_id"] == interrupted_id
    assert report["run"]["status"] == "ok"
    assert report["recovery"]["resume_count"] == 1
    assert report["progress"]["handling"] == "resume"
    assert report["recovery"]["reused_sources"] == ["rss"]
    assert report["recovery"]["processed_sources"] == ["jsonfeed"]

    another_interrupted = _interrupt_after_first_source(db_path, policy, sources)
    capsys.readouterr()
    finalize_code = main([
        "recover", "--config", str(feeds_path), "--policy", str(policy_path),
        "--db", str(db_path), "--mode", "finalize", "--quiet",
    ])
    assert finalize_code == 0
    assert "finalize" in capsys.readouterr().out

    interrupted_report = main([
        "report", "--db", str(db_path), "--out", str(out_dir),
        "--run-id", another_interrupted,
    ])
    assert interrupted_report == 0
    old = json.loads(
        (out_dir / f"fetch-report-{another_interrupted}.json").read_text("utf-8")
    )
    assert old["run"]["status"] == "interrupted"
    assert old["recovery"]["successor_run_id"] != another_interrupted


def test_cli_recover_plan_mismatch_exits_five(tmp_path, mock_server, source_factory,
                                              write_feeds, write_policy, capsys):
    from feedkeep.config import load_policy

    sources = [
        source_factory(mock_server, source_id="rss"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="jsonfeed", fmt="jsonfeed"),
    ]
    feeds_path = write_feeds(sources)
    policy_path = write_policy()
    db_path = tmp_path / "data" / "archive.sqlite3"
    _interrupt_after_first_source(db_path, load_policy(policy_path), sources)
    capsys.readouterr()

    reordered = write_feeds(list(reversed(sources)), filename="reordered.yaml")
    code = main([
        "recover", "--config", str(reordered), "--policy", str(policy_path),
        "--db", str(db_path), "--mode", "resume", "--quiet",
    ])
    assert code == 5
    assert "RECOVERY_PLAN_MISMATCH" in capsys.readouterr().err

    # A completed database has no running round, so recover has nothing to do.
    finished_db = tmp_path / "data" / "finished.sqlite3"
    assert main([
        "fetch", "--config", str(feeds_path), "--policy", str(policy_path),
        "--db", str(finished_db), "--quiet",
    ]) == 0
    capsys.readouterr()
    no_run = main([
        "recover", "--config", str(feeds_path), "--policy", str(policy_path),
        "--db", str(finished_db), "--mode", "finalize", "--quiet",
    ])
    assert no_run == 2
    assert "running" in capsys.readouterr().err

    missing_db = main([
        "recover", "--config", str(feeds_path), "--policy", str(policy_path),
        "--db", str(tmp_path / "missing.sqlite3"), "--mode", "resume", "--quiet",
    ])
    assert missing_db == 2
