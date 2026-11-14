from __future__ import annotations

import json

from feedkeep.cli import main
from feedkeep.demo import run_demo
from feedkeep.mockdata import check_fixtures


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
