"""argparse command surface and stable process exit codes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .clock import SystemClock
from .config import ConfigError, load_feeds, load_policy
from .demo import main as demo_main
from .fetcher import Fetcher
from .ledger import Ledger
from .report import ReportError, write_reports
from .store import SourceLifecycleError, Store

DEFAULT_DB = "data/feedkeep.sqlite3"
DEFAULT_CONFIG = "configs/feeds.yaml"
DEFAULT_POLICY = "configs/policy.yaml"
DEFAULT_REPORTS = "reports"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feedkeep",
        description="纯本地、配置驱动的订阅源增量采集与归档引擎",
    )
    parser.add_argument("--version", action="version", version=f"feedkeep {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="执行一轮增量抓取")
    fetch.add_argument("--config", default=DEFAULT_CONFIG, help="订阅源 YAML")
    fetch.add_argument("--policy", default=DEFAULT_POLICY, help="网络策略 YAML")
    fetch.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    fetch.add_argument("--quiet", action="store_true", help="只输出 run 摘要")

    report = sub.add_parser("report", help="从归档生成 JSON/Markdown 报告")
    report.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    report.add_argument("--out", default=DEFAULT_REPORTS, help="报告输出目录")
    report.add_argument("--run-id", default=None, help="默认使用最近一轮")

    pause = sub.add_parser("pause", help="暂停来源：停止请求，既有条目保持活跃")
    pause.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    pause.add_argument("--source", required=True, help="来源 id")

    retire = sub.add_parser("retire", help="退役来源：结束当前归档纪元，条目转入退役清单")
    retire.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    retire.add_argument("--source", required=True, help="来源 id")

    resume = sub.add_parser("resume", help="恢复来源：续用旧纪元或开启新纪元")
    resume.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    resume.add_argument("--source", required=True, help="来源 id")
    resume.add_argument("--mode", choices=("epoch", "new-epoch"), default="epoch",
                        help="epoch=恢复原纪元（默认）；new-epoch=保留旧档并开启新纪元")

    demo = sub.add_parser("demo", help="启动本地 mock 服务并完成自检")
    demo.add_argument("--out", default="reports/demo", help="demo 报告目录")
    demo.add_argument("--port", type=int, default=0, help="mock 端口，0 表示自动分配")
    return parser


def _print_config_error(exc: ConfigError) -> None:
    for issue in exc.issues:
        print(str(issue), file=sys.stderr)


def command_fetch(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.policy)
        sources = load_feeds(args.config, policy)
    except ConfigError as exc:
        _print_config_error(exc)
        return 3
    clock = SystemClock()
    with Store(args.db) as store:
        ledger = Ledger(store, policy, Fetcher(policy, clock), clock)
        summary = ledger.run(sources, args.config)
    if not args.quiet:
        print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{summary.run_id} {summary.status} "
              f"ok={summary.counts.sources_ok} "
              f"not_modified={summary.counts.sources_not_modified} "
              f"failed={summary.counts.sources_failed}")
    return {"ok": 0, "partial": 1, "failed": 2}[summary.status]


def command_report(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2
    with Store(args.db) as store:
        try:
            json_path, markdown_path, report = write_reports(
                store, args.out, args.run_id, SystemClock()
            )
        except ReportError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print(f"run={report['run']['run_id']} status={report['run']['status']}")
    return 0


def command_lifecycle(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2
    clock = SystemClock()
    with Store(args.db) as store:
        try:
            if args.command == "pause":
                result = store.pause_source(args.source, clock.now_iso())
            elif args.command == "retire":
                result = store.retire_source(args.source, clock.now_iso())
            else:
                result = store.resume_source(args.source, clock.now_iso(), args.mode)
        except SourceLifecycleError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_demo(args: argparse.Namespace) -> int:
    return demo_main(out_dir=args.out, port=args.port)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            return command_fetch(args)
        if args.command == "report":
            return command_report(args)
        if args.command in ("pause", "retire", "resume"):
            return command_lifecycle(args)
        if args.command == "demo":
            return command_demo(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
