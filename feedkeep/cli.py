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
from .identity import IDENTITY_CONTRACTS
from .ledger import Ledger
from .migration import MigrationBlocked, apply_migration, plan_migration
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

    demo = sub.add_parser("demo", help="启动本地 mock 服务并完成自检")
    demo.add_argument("--out", default="reports/demo", help="demo 报告目录")
    demo.add_argument("--port", type=int, default=0, help="mock 端口，0 表示自动分配")

    common_identity = argparse.ArgumentParser(add_help=False)
    common_identity.add_argument("--config", default=DEFAULT_CONFIG, help="订阅源 YAML")
    common_identity.add_argument("--policy", default=DEFAULT_POLICY, help="网络策略 YAML")
    common_identity.add_argument("--db", default=DEFAULT_DB, help="SQLite 归档文件")
    common_identity.add_argument("--source", action="append", dest="sources",
                                 help="只处理指定源 id，可重复；默认处理配置中的全部源")
    common_identity.add_argument("--json", dest="as_json", action="store_true",
                                 help="以 JSON 输出机器可读报告")

    plan = sub.add_parser(
        "identity-plan",
        parents=[common_identity],
        help="只读生成身份契约迁移冲突清单（不改动数据库）",
    )
    plan.add_argument("--out", default=None, help="可选：把报告写入文件而不是标准输出")

    migrate = sub.add_parser(
        "identity-migrate",
        parents=[common_identity],
        help="显式应用身份契约迁移（存在冲突时拒绝执行）",
    )
    migrate.add_argument("--out", default=None, help="可选：把报告写入文件而不是标准输出")
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


def command_demo(args: argparse.Namespace) -> int:
    return demo_main(out_dir=args.out, port=args.port)


def _load_identity_targets(args: argparse.Namespace) -> tuple[dict[str, str], int]:
    """Return ``{source_id: target_contract}`` from the feed configuration."""

    try:
        policy = load_policy(args.policy)
        sources = load_feeds(args.config, policy)
    except ConfigError as exc:
        _print_config_error(exc)
        return {}, 3
    if args.sources:
        known = {source.id for source in sources}
        unknown = [source_id for source_id in args.sources if source_id not in known]
        if unknown:
            print(f"配置中不存在的源 id: {', '.join(unknown)}", file=sys.stderr)
            return {}, 3
        sources = [source for source in sources if source.id in set(args.sources)]
    return {source.id: source.identity_contract for source in sources}, 0


def _emit_report(payload: dict, args: argparse.Namespace) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"报告已写入: {args.out}")
    else:
        print(text)


def _render_identity_text(payload: dict) -> str:
    lines = [
        "身份契约迁移报告",
        f"- 来源总数: {payload['sources_total']}",
        f"- 已对齐（无需迁移）: {payload['sources_aligned']}",
        f"- 存在冲突的来源: {payload['sources_with_conflicts']}",
        f"- 待迁移条目数: {payload['items_to_migrate']}",
        "",
    ]
    for plan in payload["plans"]:
        lines.append(
            f"[{plan['source_id']}] {plan['from_contract']} -> {plan['to_contract']} "
            f"条目={plan['items_total']} 待迁移={plan['items_to_migrate']} "
            f"状态={'已对齐' if plan['aligned'] else ('冲突阻止' if plan['blocked'] else '可迁移')}"
        )
        for mapping in plan["mappings"]:
            if mapping["changed"]:
                lines.append(
                    f"  - {mapping['old_item_key']} -> {mapping['new_item_key']} "
                    f"({mapping['identity_kind']})"
                )
    if payload["conflicts"]:
        lines.extend(["", "阻塞冲突（未改动任何数据）:"])
        for conflict in payload["conflicts"]:
            lines.append(
                f"  ! {conflict['code']} source={conflict['source_id']} "
                f"old={conflict['old_item_keys']} new={conflict['new_item_keys']}"
            )
            lines.append(f"    原因: {conflict['reason']}")
    return "\n".join(lines)


def command_identity_plan(args: argparse.Namespace) -> int:
    targets, error = _load_identity_targets(args)
    if error:
        return error
    with Store(args.db) as store:
        report = plan_migration(store, targets)
        payload = report.as_dict()
        payload["current_contracts"] = {
            source_id: store.get_identity_contract(source_id) for source_id in sorted(targets)
        }
    if args.as_json:
        _emit_report(payload, args)
    else:
        text = _render_identity_text(payload)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"报告已写入: {args.out}")
        else:
            print(text)
    return 4 if report.blocked else 0


def command_identity_migrate(args: argparse.Namespace) -> int:
    targets, error = _load_identity_targets(args)
    if error:
        return error
    observed_at = SystemClock().now_iso()
    with Store(args.db) as store:
        try:
            report = apply_migration(store, targets, observed_at)
        except MigrationBlocked as exc:
            payload = exc.report.as_dict()
            payload["applied"] = False
            payload["migrated_items"] = 0
            if args.as_json:
                _emit_report(payload, args)
            else:
                print(_render_identity_text(payload), file=sys.stderr)
                print(str(exc), file=sys.stderr)
            return 4
        payload = report.as_dict()
        payload["applied"] = True
        payload["migrated_items"] = sum(
            plan.changed_count for plan in report.plans if not plan.aligned
        )
        payload["applied_at"] = observed_at
        payload["migration_ledger"] = store.get_migration_records()
    if args.as_json:
        _emit_report(payload, args)
    else:
        text = _render_identity_text(payload)
        text += (
            f"\n\n已应用迁移: {payload['migrated_items']} 个条目，"
            f"时间 {observed_at}；原始素材未改动。"
        )
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"报告已写入: {args.out}")
        else:
            print(text)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            return command_fetch(args)
        if args.command == "report":
            return command_report(args)
        if args.command == "demo":
            return command_demo(args)
        if args.command == "identity-plan":
            return command_identity_plan(args)
        if args.command == "identity-migrate":
            return command_identity_migrate(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
