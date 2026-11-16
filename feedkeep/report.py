"""Stable JSON and Markdown report rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .clock import Clock, SystemClock
from .store import Store

SCHEMA_VERSION = 2
RUN_FIELDS = (
    "run_id", "started_at", "finished_at", "status", "config_path",
    "sources_total", "sources_ok", "sources_not_modified", "sources_failed",
    "items_seen", "items_new", "items_updated", "items_missing",
    "items_restored", "items_retracted", "item_errors",
)
SOURCE_FIELDS = (
    "source_id", "source_name", "status", "http_status", "duration_ms", "body_bytes",
    "body_sha256", "etag", "last_modified", "attempts", "items_total", "items_new",
    "items_updated", "items_missing", "items_restored", "items_retracted", "item_errors",
    "error_code", "error_message", "truncated", "observed_at",
)


class ReportError(Exception):
    pass


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = {field: row.get(field) for field in fields}
    if "truncated" in result:
        result["truncated"] = bool(result["truncated"])
    return result


def build_report(store: Store, run_id: Optional[str] = None,
                 clock: Optional[Clock] = None) -> dict[str, Any]:
    selected = run_id or store.latest_run_id()
    if not selected:
        raise ReportError("没有可报告的已完成抓取记录")
    row = store.get_run(selected)
    if row is None:
        raise ReportError(f"找不到抓取记录: {selected}")
    if row.get("status") == "running":
        raise ReportError(f"抓取记录尚未完成: {selected}")
    run = _pick(row, RUN_FIELDS)
    run["run_id"] = row["id"]
    sources = [_pick(item, SOURCE_FIELDS) for item in store.get_source_runs(selected)]
    sources.sort(key=lambda item: item["source_id"])
    events = store.get_events(selected)
    events.sort(key=lambda item: (
        item["source_id"], item["item_key"], item["event_type"], item["observed_at"],
    ))
    current_clock = clock or SystemClock()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": current_clock.now_iso(),
        "run": run,
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
    lines = [
        f"# feedkeep 抓取报告：{run['run_id']}",
        "",
        f"- 状态：`{run['status']}`",
        f"- 开始：{run['started_at']}",
        f"- 结束：{run['finished_at']}",
        f"- 配置：{_cell(run['config_path'])}",
        f"- 报告生成：{report['generated_at']}",
        "",
        "## 汇总",
        "",
        _line(["来源", "成功", "未修改", "失败", "条目", "新增", "更新", "缺失", "恢复", "撤回", "条目错误"]),
        _line(["---"] * 11),
        _line([
            run["sources_total"], run["sources_ok"], run["sources_not_modified"],
            run["sources_failed"], run["items_seen"], run["items_new"],
            run["items_updated"], run["items_missing"], run["items_restored"],
            run["items_retracted"], run["item_errors"],
        ]),
        "",
        "## 来源明细",
        "",
        _line(["来源", "状态", "HTTP", "尝试", "字节", "耗时ms", "条目", "新增", "更新",
               "缺失", "恢复", "撤回", "错误"]),
        _line(["---"] * 13),
    ]
    for source in report["sources"]:
        lines.append(_line([
            source["source_id"], source["status"], source["http_status"],
            source["attempts"], source["body_bytes"], source["duration_ms"],
            source["items_total"], source["items_new"], source["items_updated"],
            source["items_missing"], source["items_restored"],
            source["items_retracted"], source["item_errors"],
        ]))
    if not report["sources"]:
        lines.append("| （无启用来源） | - | - | - | - | - | - | - | - | - | - | - | - |")
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
    lines.append(_line(["来源", "总数", "活跃", "缺失", "撤回"]))
    lines.append(_line(["---"] * 5))
    for row in report["inventory"]:
        lines.append(_line([
            row["source_id"], row["total"], row["active"], row["missing"],
            row["retracted"],
        ]))
    if not report["inventory"]:
        lines.append("| （无） | - | - | - | - |")
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
