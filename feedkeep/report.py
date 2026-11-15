"""Stable JSON and Markdown report rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .clock import Clock, SystemClock
from .recovery import enabled_source_ids
from .store import Store

SCHEMA_VERSION = 2
RUN_FIELDS = (
    "run_id", "started_at", "finished_at", "status", "config_path",
    "sources_total", "sources_ok", "sources_not_modified", "sources_failed",
    "items_seen", "items_new", "items_updated", "items_missing",
    "items_restored", "item_errors",
)
RECOVERY_FIELDS = (
    "recovery_action", "recovery_at", "interrupted_at", "resume_count",
    "prior_run_id", "successor_run_id",
)
SOURCE_FIELDS = (
    "source_id", "source_name", "status", "http_status", "duration_ms", "body_bytes",
    "body_sha256", "etag", "last_modified", "attempts", "items_total", "items_new",
    "items_updated", "items_missing", "items_restored", "item_errors",
    "error_code", "error_message", "truncated", "observed_at",
)


class ReportError(Exception):
    pass


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = {field: row.get(field) for field in fields}
    if "truncated" in result:
        result["truncated"] = bool(result["truncated"])
    return result


def _source_view(row: dict[str, Any], recorded_ids: set[str],
                 processed_ids: set[str], run_status: str) -> dict[str, Any]:
    source = _pick(row, SOURCE_FIELDS)
    if row["source_id"] in processed_ids:
        source["processing"] = "processed"
    elif row["source_id"] in recorded_ids:
        source["processing"] = "resumed"
    elif run_status == "interrupted":
        source["processing"] = "recorded"
    else:
        source["processing"] = "processed"
    return source


def _recovery_view(store: Store, row: dict[str, Any]) -> dict[str, Any]:
    view = {field: row.get(field) for field in RECOVERY_FIELDS}
    view["reused_sources"] = store.get_recovery_sources(row["id"], "reused")
    view["processed_sources"] = store.get_recovery_sources(row["id"], "processed")
    return view


def _progress_view(row: dict[str, Any], sources: list[dict[str, Any]],
                   snapshot: Any) -> dict[str, Any]:
    status = row.get("status")
    action = row.get("recovery_action")
    interrupted = status in {"interrupted", "running"} or action == "resume"
    configured_ids = enabled_source_ids(snapshot) if snapshot else [
        source["source_id"] for source in sources
    ]
    done = {source["source_id"] for source in sources}
    pending = [source_id for source_id in configured_ids if source_id not in done]
    if status == "running":
        reason = "尚未结束（等待显式 recover 继续或定格）"
    elif status == "interrupted":
        reason = "被显式定格为中断轮次，保留已完成来源事实"
    elif action == "resume":
        reason = "经显式 recover --mode resume 继续完成"
    elif action == "finalize" and row.get("prior_run_id"):
        reason = "由显式 recover --mode finalize 重开的新轮次"
    else:
        reason = None
    return {
        "interrupted": interrupted,
        "reason": reason,
        "handling": action,
        "interrupted_at": row.get("interrupted_at"),
        "configured_sources": configured_ids,
        "completed_sources": [source["source_id"] for source in sources],
        "pending_sources": pending,
    }


def build_report(store: Store, run_id: Optional[str] = None,
                 clock: Optional[Clock] = None) -> dict[str, Any]:
    selected = run_id or store.latest_run_id()
    if not selected:
        raise ReportError("没有可报告的已完成抓取记录")
    row = store.get_run(selected)
    if row is None:
        raise ReportError(f"找不到抓取记录: {selected}")
    if row.get("status") == "running":
        raise ReportError(
            f"抓取记录尚未完成: {selected}（请使用 feedkeep recover 继续或定格该轮次）"
        )
    run = _pick(row, RUN_FIELDS)
    run["run_id"] = row["id"]
    recovery = _recovery_view(store, row)
    raw_sources = store.get_source_runs(selected)
    recorded_ids = set(recovery["reused_sources"])
    processed_ids = set(recovery["processed_sources"])
    sources = [
        _source_view(source, recorded_ids, processed_ids, row.get("status"))
        for source in raw_sources
    ]
    sources.sort(key=lambda item: item["source_id"])
    snapshot = store.get_run_snapshot(selected)
    events = store.get_events(selected)
    events.sort(key=lambda item: (
        item["source_id"], item["item_key"], item["event_type"], item["observed_at"],
    ))
    current_clock = clock or SystemClock()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": current_clock.now_iso(),
        "run": run,
        "recovery": recovery,
        "progress": _progress_view(row, sources, snapshot),
        "sources": sources,
        "item_issues": store.get_item_issues(selected),
        "events": events,
        "inventory": store.inventory_for_run(selected),
    }


def render_json(report: dict[str, Any]) -> bytes:
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text if text else "-"


def _line(values: list[Any]) -> str:
    return "| " + " | ".join(_cell(value) for value in values) + " |"


def render_markdown(report: dict[str, Any]) -> bytes:
    run = report["run"]
    recovery = report.get("recovery") or {}
    progress = report.get("progress") or {}
    lines = [
        f"# feedkeep 抓取报告：{run['run_id']}",
        "",
        f"- 状态：`{run['status']}`",
        f"- 开始：{run['started_at']}",
        f"- 结束：{run['finished_at']}",
        f"- 配置：{_cell(run['config_path'])}",
        f"- 报告生成：{report['generated_at']}",
    ]
    if progress.get("interrupted") or recovery.get("recovery_action"):
        lines.extend([
            "",
            "## 中断与恢复",
            "",
            f"- 中断：{'是' if progress.get('interrupted') else '否'}",
            f"- 处理方式：`{_cell(progress.get('handling'))}`",
            f"- 说明：{_cell(progress.get('reason'))}",
        ])
        if recovery.get("prior_run_id"):
            lines.append(f"- 前序中断轮次：{_cell(recovery['prior_run_id'])}")
        if recovery.get("successor_run_id"):
            lines.append(f"- 后续重开轮次：{_cell(recovery['successor_run_id'])}")
        if recovery.get("interrupted_at"):
            lines.append(f"- 定格时间：{_cell(recovery['interrupted_at'])}")
        if recovery.get("recovery_at"):
            lines.append(f"- 恢复时间：{_cell(recovery['recovery_at'])}")
        if recovery.get("resume_count"):
            lines.append(f"- 续跑次数：{recovery['resume_count']}")
        lines.extend([
            f"- 已完成来源：{_cell(', '.join(progress.get('completed_sources') or []))}",
            f"- 未完成来源：{_cell(', '.join(progress.get('pending_sources') or []))}",
        ])
        reused = recovery.get("reused_sources") or []
        if reused:
            lines.append(f"- 沿用旧结果的来源：{_cell(', '.join(reused))}")
        processed = recovery.get("processed_sources") or []
        if processed:
            lines.append(f"- 本次处理的来源：{_cell(', '.join(processed))}")
    lines.extend([
        "",
        "## 汇总",
        "",
        _line(["来源", "成功", "未修改", "失败", "条目", "新增", "更新", "缺失", "恢复", "条目错误"]),
        _line(["---", "---", "---", "---", "---", "---", "---", "---", "---", "---"]),
        _line([
            run["sources_total"], run["sources_ok"], run["sources_not_modified"],
            run["sources_failed"], run["items_seen"], run["items_new"],
            run["items_updated"], run["items_missing"], run["items_restored"],
            run["item_errors"],
        ]),
        "",
        "## 来源明细",
        "",
        _line(["来源", "状态", "处理", "HTTP", "尝试", "字节", "耗时ms", "条目", "新增", "更新", "缺失", "恢复", "错误"]),
        _line(["---"] * 13),
    ])
    for source in report["sources"]:
        lines.append(_line([
            source["source_id"], source["status"], source["processing"],
            source["http_status"], source["attempts"], source["body_bytes"],
            source["duration_ms"], source["items_total"], source["items_new"],
            source["items_updated"], source["items_missing"],
            source["items_restored"], source["item_errors"],
        ]))
    if not report["sources"]:
        lines.append("| （无来源记录） | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(["", "## 来源错误", ""])
    failures = [source for source in report["sources"] if source["error_code"]]
    if failures:
        lines.extend([_line(["来源", "错误码", "HTTP", "消息"]), _line(["---"] * 4)])
        for source in failures:
            lines.append(_line([
                source["source_id"], source["error_code"], source["http_status"],
                source["error_message"],
            ]))
    else:
        lines.append("无。")
    lines.extend(["", "## 条目级错误", ""])
    if report["item_issues"]:
        lines.extend([_line(["来源", "序号", "错误码", "消息"]), _line(["---"] * 4)])
        for issue in report["item_issues"]:
            lines.append(_line([
                issue["source_id"], issue["item_index"], issue["code"], issue["message"],
            ]))
    else:
        lines.append("无。")
    lines.extend(["", "## 生命周期事件", ""])
    if report["events"]:
        lines.extend([_line(["来源", "条目", "事件", "时间"]), _line(["---"] * 4)])
        for event in report["events"]:
            lines.append(_line([
                event["source_id"], event["item_key"], event["event_type"],
                event["observed_at"],
            ]))
    else:
        lines.append("无。")
    lines.extend(["", "## 当前归档清单", ""])
    lines.extend([_line(["来源", "总数", "活跃", "缺失"]), _line(["---"] * 4)])
    for row in report["inventory"]:
        lines.append(_line([row["source_id"], row["total"], row["active"], row["missing"]]))
    if not report["inventory"]:
        lines.append("| （无） | - | - | - |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_reports(store: Store, out_dir: str | Path, run_id: Optional[str] = None,
                  clock: Optional[Clock] = None) -> tuple[Path, Path, dict[str, Any]]:
    report = build_report(store, run_id, clock)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"fetch-report-{report['run']['run_id']}"
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_bytes(render_json(report))
    markdown_path.write_bytes(render_markdown(report))
    return json_path, markdown_path, report
