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
from .recovery import RecoveryError
from .report import ReportError, write_reports
from .store import Store

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

    recover = sub.add_parser(
        "recover",
        help="显式处理中断轮次：继续同一轮或定格后重开",
        description=(
            "显式选择中断轮次的处理方式。resume 仅在配置来源集合、顺序与策略摘要"
            "完全一致时复用旧 run_id 并只处理剩余来源；finalize 把旧轮定格为 "
            "interrupted 后创建新的完整轮次。"
        ),
    )
    recover.add_argument("--config", default=DEFAULT_CONFIG, help="订阅源 YAML")
    recover.add_argument("--policy", default=DEFAULT_POLICY, help="网络策略 YAML")
    recover.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    recover.add_argument("--run-id", default=None,
                         help="中断轮次 id；默认选择最近一个 running 轮次")
    recover.add_argument("--mode", choices=("resume", "finalize"), required=True,
                         help="resume=继续同一轮；finalize=定格并重开")
    recover.add_argument("--quiet", action="store_true", help="只输出一行摘要")

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


def _select_interrupted_run(store: Store, run_id: Optional[str]) -> Optional[str]:
    if run_id:
        return run_id
    row = store.running_run()
    return row["id"] if row else None


def command_recover(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2
    try:
        policy = load_policy(args.policy)
        sources = load_feeds(args.config, policy)
    except ConfigError as exc:
        _print_config_error(exc)
        return 3
    clock = SystemClock()
    with Store(args.db) as store:
        run_id = _select_interrupted_run(store, args.run_id)
        if run_id is None:
            print("没有处于 running 状态的中断轮次", file=sys.stderr)
            return 2
        ledger = Ledger(store, policy, Fetcher(policy, clock), clock)
        try:
            if args.mode == "resume":
                outcome = ledger.resume(run_id, sources, args.config)
            else:
                outcome = ledger.finalize(run_id, sources, args.config)
        except RecoveryError as exc:
            print(str(exc), file=sys.stderr)
            return 5
    if args.quiet:
        print(f"{outcome.run_id} {outcome.action} "
              f"reused={len(outcome.reused_source_ids)} "
              f"processed={len(outcome.processed_source_ids)}")
    else:
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if outcome.summary is None:
        return 0
    return {"ok": 0, "partial": 1, "failed": 2}[outcome.summary.status]


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
        if args.command == "recover":
            return command_recover(args)
        if args.command == "demo":
            return command_demo(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
