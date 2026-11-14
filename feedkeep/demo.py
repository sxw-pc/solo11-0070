"""Self-checking offline demo: mock server -> two fetches -> reports."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

from mockserver.server import running_mock_server

from .clock import FixedClock
from .config import load_feeds, load_policy
from .fetcher import Fetcher
from .ledger import Ledger
from .mockdata import check_fixtures
from .report import write_reports
from .store import Store

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FEEDS = PROJECT_ROOT / "configs" / "feeds.yaml"
DEFAULT_POLICY = PROJECT_ROOT / "configs" / "policy.yaml"


class DemoCheckError(Exception):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoCheckError(message)


def _port_rewritten_config(path: Path, destination: Path, base_url: str) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for feed in data["feeds"]:
        if isinstance(feed.get("url"), str):
            feed["url"] = feed["url"].replace("http://127.0.0.1:8765", base_url)
    destination.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def run_demo(out_dir: str | Path = "reports/demo", port: int = 0,
             feeds_path: str | Path = DEFAULT_FEEDS,
             policy_path: str | Path = DEFAULT_POLICY) -> dict[str, Any]:
    """Run the full offline workflow and return a machine-readable summary."""

    fixture_problems = check_fixtures()
    _check(not fixture_problems, "fixture 校验失败: " + ", ".join(fixture_problems))
    policy = load_policy(policy_path)
    clock = FixedClock("2024-02-01T00:00:00Z", monotonic_start=5000.0)
    with tempfile.TemporaryDirectory(prefix="feedkeep-demo-") as temp_name, \
            running_mock_server(port=port) as server:
        temp = Path(temp_name)
        configured = temp / "feeds.yaml"
        _port_rewritten_config(Path(feeds_path), configured, server.base_url)
        sources = load_feeds(configured, policy)
        store = Store(temp / "demo.sqlite3")
        try:
            ledger = Ledger(store, policy, Fetcher(policy, clock), clock)
            first = ledger.run(sources, str(configured))
            second = ledger.run(sources, str(configured))
            _check(first.counts.sources_total == 6, "demo 配置应包含 6 个来源")
            _check(first.counts.sources_failed == 1, "第一轮应恰有一个源级失败")
            _check(first.counts.items_new > 0, "第一轮应归档新条目")
            _check(first.counts.item_errors >= 3, "第一轮应记录条目级错误")
            _check(second.status == "partial", "第二轮因坏源应仍为 partial")
            _check(second.counts.sources_not_modified >= 4, "第二轮应出现条件请求 304")
            _check(second.counts.items_new == 0, "第二轮不应重复新增条目")
            _check(second.counts.items_missing == 0, "第二轮不应误报缺失")
            malformed = next(s for s in first.sources if s.source_id == "rss-malformed")
            _check(malformed.error_code == "PARSE_INVALID_XML", "坏源应记录 PARSE_INVALID_XML")
            conditional = [
                request for request in server.snapshot()
                if request["if_none_match"] is not None
            ]
            _check(bool(conditional), "第二轮应发送 If-None-Match")
            _check(any(request["status"] == 304 for request in conditional), "服务端应返回 304")
            first_json, _, _ = write_reports(
                store, Path(out_dir) / "first-run", first.run_id, clock
            )
            first_report = json.loads(first_json.read_text(encoding="utf-8"))
            _check(bool(first_report["item_issues"]), "第一轮报告应包含条目级错误")
            json_path, markdown_path, _ = write_reports(
                store, out_dir, second.run_id, clock
            )
            _check(json_path.exists() and markdown_path.exists(), "报告文件未生成")
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            _check(parsed["run"]["run_id"] == second.run_id, "JSON 报告 run 不正确")
            _check(len(parsed["sources"]) == 6, "报告应包含 6 个来源")
            _check(markdown_path.stat().st_size > 0, "Markdown 报告为空")
            return {
                "first_run": first.as_dict(),
                "second_run": second.as_dict(),
                "first_json_report": str(first_json),
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                "fixtures_checked": 9,
            }
        finally:
            store.close()


def main(out_dir: str | Path = "reports/demo", port: int = 0) -> int:
    try:
        summary = run_demo(out_dir=out_dir, port=port)
    except DemoCheckError as exc:
        print(f"demo self-check failed: {exc}")
        return 1
    except Exception as exc:
        print(f"demo failed: {type(exc).__name__}: {exc}")
        return 1
    second = summary["second_run"]
    print("feedkeep demo: ok")
    print(f"  run 1: {summary['first_run']['status']} "
          f"(new={summary['first_run']['items_new']}, failed={summary['first_run']['sources_failed']})")
    print(f"  run 2: {second['status']} "
          f"(not_modified={second['sources_not_modified']}, new={second['items_new']})")
    print(f"  json: {summary['json_report']}")
    print(f"  markdown: {summary['markdown_report']}")
    return 0
