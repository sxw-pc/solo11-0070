"""Stable JSON and Markdown report rendering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .clock import Clock, SystemClock
from .store import Store

SCHEMA_VERSION = 1
GLOBAL_SCHEMA_VERSION = 1

# 跨源谱系视图的统计口径说明，随报告输出固定下发。
URL_VIEW_BASIS = (
    "按完整规范化 link 分组，同一 URL 视为一个页面谱系，其下不同 content_hash "
    "是该页面地址的版本候选；成员资格按每来源最近一次 source_run 状态（完整成功 / "
    "304 / 失败 / 截断）与条目 missing 状态分别判定，同时间的竞争版本并列标注为冲突、"
    "不裁决胜者，不做跨源正文覆盖。"
)
SNAPSHOT_VIEW_BASIS = (
    "每个 (normalized_url, content_hash) 视为一个独立内容快照，按可复核字节指纹计数；"
    "地址相同但内容不同即为不同快照，不把地址相同当成内容相同。"
)
SOURCE_LOCAL_BASIS = (
    "无可用 link 的条目维持 source-local 身份，不参与跨源关联，"
    "也不按标题相似度猜测合并。"
)
RUN_FIELDS = (
    "run_id", "started_at", "finished_at", "status", "config_path",
    "sources_total", "sources_ok", "sources_not_modified", "sources_failed",
    "items_seen", "items_new", "items_updated", "items_missing",
    "items_restored", "item_errors",
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
        _line(["来源", "状态", "HTTP", "尝试", "字节", "耗时ms", "条目", "新增", "更新", "缺失", "恢复", "错误"]),
        _line(["---"] * 12),
    ]
    for source in report["sources"]:
        lines.append(_line([
            source["source_id"], source["status"], source["http_status"],
            source["attempts"], source["body_bytes"], source["duration_ms"],
            source["items_total"], source["items_new"], source["items_updated"],
            source["items_missing"], source["items_restored"], source["item_errors"],
        ]))
    if not report["sources"]:
        lines.append("| （无启用来源） | - | - | - | - | - | - | - | - | - | - | - |")
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


# ---------------------------------------------------------------------------
# 跨源条目谱系（global-report）：纯派生、只读，不改 items 主键与 source-local 身份
# ---------------------------------------------------------------------------

_IDENTITY_KIND_PREFIX = {"id": "id", "link": "link", "td": "title_date"}


def _identity_kind(item_key: str) -> str:
    prefix = item_key.split(":", 1)[0]
    return _IDENTITY_KIND_PREFIX.get(prefix, "unknown")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _hash_timeline(row: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct the ordered content-hash history of one source-local item.

    Hashes only enter via full successful observations: ``discovered`` /
    ``updated`` events carry the newly observed hash and their run id.
    ``missing`` / ``restored`` carry no new byte content, and 304/failed runs
    never produce item events, so they cannot manufacture a version here.
    Consecutive equal hashes collapse into one timeline entry whose
    ``last_observed_run_id`` advances.
    """

    timeline: list[dict[str, Any]] = []
    for event in events:
        if event["event_type"] not in ("discovered", "updated"):
            continue
        content_hash = event["detail"].get("content_hash")
        if not content_hash:
            continue
        if timeline and timeline[-1]["content_hash"] == content_hash:
            timeline[-1]["last_observed_run_id"] = event["run_id"]
            timeline[-1]["last_observed_at"] = event["observed_at"]
        else:
            timeline.append({
                "content_hash": content_hash,
                "first_observed_run_id": event["run_id"],
                "first_observed_at": event["observed_at"],
                "last_observed_run_id": event["run_id"],
                "last_observed_at": event["observed_at"],
            })
    current_hash = row["content_hash"]
    if timeline and timeline[-1]["content_hash"] == current_hash:
        timeline[-1]["last_observed_run_id"] = row["last_run_id"]
        timeline[-1]["last_observed_at"] = row["last_seen_at"]
    else:
        timeline.append({
            "content_hash": current_hash,
            "first_observed_run_id": row["last_run_id"],
            "first_observed_at": row["last_seen_at"],
            "last_observed_run_id": row["last_run_id"],
            "last_observed_at": row["last_seen_at"],
        })
    return timeline


def _membership_state(row: dict[str, Any], latest_runs: dict[str, dict[str, Any]],
                      complete_runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Combine item missing state with per-source run state into one membership.

    Only an explicit ``missing_since`` (set by a full, non-truncated successful
    run) ends an active association. Failures, 304s and truncations downgrade
    the *currency* of the last-known membership but never delete it.
    """

    source_id = row["source_id"]
    latest = latest_runs.get(source_id)
    complete = complete_runs.get(source_id)
    if row["missing_since"] is not None:
        return {
            "state": "missing",
            "currency": "ended",
            "currency_reason": "完整成功轮次中明确缺席",
            "latest_source_run_id": latest["run_id"] if latest else None,
            "latest_source_run_status": latest["status"] if latest else None,
            "latest_complete_run_id": complete["run_id"] if complete else None,
        }

    if latest is None:
        return {
            "state": "active",
            "currency": "unknown",
            "currency_reason": "该来源尚无 source_run 记录",
            "latest_source_run_id": None,
            "latest_source_run_status": None,
            "latest_complete_run_id": complete["run_id"] if complete else None,
        }

    if latest["status"] == "failed":
        currency = ("stale", "最近来源轮次失败，last-known 关联保留但无法确认当前性")
    elif latest["status"] == "not_modified":
        currency = ("stale", "最近来源轮次为 304 未修改，last-known 关联保留但无法确认当前性")
    elif latest["truncated"]:
        currency = ("stale", "最近来源轮次被截断，被省略的版本不得推断为消失")
    elif latest["status"] == "ok" and not latest["truncated"]:
        currency = ("current", "最近来源轮次为完整成功观察，且该条目处于活跃状态")
    else:
        currency = ("unknown", "来源轮次状态无法解释成员当前性")

    return {
        "state": "active",
        "currency": currency[0],
        "currency_reason": currency[1],
        "latest_source_run_id": latest["run_id"],
        "latest_source_run_status": latest["status"],
        "latest_complete_run_id": complete["run_id"] if complete else None,
    }


def _build_membership(row: dict[str, Any], events: list[dict[str, Any]],
                      latest_runs: dict[str, dict[str, Any]],
                      complete_runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    state = _membership_state(row, latest_runs, complete_runs)
    timeline = _hash_timeline(row, events)
    membership = {
        "source_id": row["source_id"],
        "item_key": row["item_key"],
        "identity_kind": _identity_kind(row["item_key"]),
        "title": row["title"],
        "link": row["link"],
        "content_hash": row["content_hash"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "first_run_id": row["first_run_id"],
        "last_run_id": row["last_run_id"],
        "missing_since": row["missing_since"],
        "hash_history": timeline,
    }
    membership.update(state)
    return membership


def build_global_report(store: Store, clock: Optional[Clock] = None) -> dict[str, Any]:
    """Derive the read-only cross-source lineage index from the archive.

    No table is written and no source-local key is altered. Two explicit
    counting views are emitted: URL page lineages and byte-content snapshots.
    """

    if store.latest_run_id() is None:
        raise ReportError("没有可报告的已完成抓取记录")

    items = store.get_all_items()
    all_events = store.get_all_item_events()
    latest_runs = {row["source_id"]: row for row in store.get_latest_source_runs()}
    complete_runs = store.get_latest_complete_runs()

    events_by_item: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in all_events:
        events_by_item.setdefault((event["source_id"], event["item_key"]), []).append(event)

    linked: dict[str, list[dict[str, Any]]] = {}
    source_local: list[dict[str, Any]] = []
    for row in items:
        membership = _build_membership(
            row,
            events_by_item.get((row["source_id"], row["item_key"]), []),
            latest_runs,
            complete_runs,
        )
        if row["link"]:
            linked.setdefault(row["link"], []).append(membership)
        else:
            source_local.append(membership)

    url_lineages: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    for url in sorted(linked):
        members = sorted(linked[url], key=lambda item: (item["source_id"], item["item_key"]))
        lineage_id = _stable_id("page", url)

        active_members = [m for m in members if m["state"] == "active"]
        current_hashes = sorted({m["content_hash"] for m in active_members})
        all_hashes = sorted({
            entry["content_hash"]
            for member in members
            for entry in member["hash_history"]
        })

        versions: list[dict[str, Any]] = []
        for content_hash in all_hashes:
            holders = [
                member for member in members
                if any(entry["content_hash"] == content_hash
                       for entry in member["hash_history"])
            ]
            current_holders = [
                member for member in holders
                if member["state"] == "active" and member["content_hash"] == content_hash
            ]
            evidence = [
                entry
                for member in holders
                for entry in member["hash_history"]
                if entry["content_hash"] == content_hash
            ]
            evidence.sort(key=lambda entry: (
                entry["first_observed_run_id"], entry["last_observed_run_id"],
            ))
            first_evidence = min(evidence, key=lambda entry: entry["first_observed_run_id"])
            last_evidence = max(evidence, key=lambda entry: entry["last_observed_run_id"])
            versions.append({
                "content_hash": content_hash,
                "snapshot_id": _stable_id("snapshot", url, content_hash),
                "is_current": bool(current_holders),
                "current_sources": sorted(m["source_id"] for m in current_holders),
                "holder_sources": sorted(m["source_id"] for m in holders),
                "members": sorted(
                    (m for m in holders),
                    key=lambda item: (item["source_id"], item["item_key"]),
                ),
                "first_observed_run_id": first_evidence["first_observed_run_id"],
                "first_observed_at": first_evidence["first_observed_at"],
                "last_observed_run_id": last_evidence["last_observed_run_id"],
                "last_observed_at": last_evidence["last_observed_at"],
            })
            snapshots.append({
                "snapshot_id": _stable_id("snapshot", url, content_hash),
                "normalized_url": url,
                "content_hash": content_hash,
                "is_current": bool(current_holders),
                "member_sources": sorted(m["source_id"] for m in holders),
                "current_sources": sorted(m["source_id"] for m in current_holders),
                "members": sorted(
                    holders,
                    key=lambda item: (item["source_id"], item["item_key"]),
                ),
                "first_observed_run_id": first_evidence["first_observed_run_id"],
                "first_observed_at": first_evidence["first_observed_at"],
                "last_observed_run_id": last_evidence["last_observed_run_id"],
                "last_observed_at": last_evidence["last_observed_at"],
            })

        has_active = bool(active_members)
        is_conflict = len(current_hashes) > 1
        if not has_active:
            lineage_status = "inactive"
        elif is_conflict:
            lineage_status = "conflict"
        else:
            lineage_status = "consistent"
        url_lineages.append({
            "lineage_id": lineage_id,
            "normalized_url": url,
            "status": lineage_status,
            "has_conflicting_versions": is_conflict,
            "active_source_count": len(active_members),
            "member_source_count": len({m["source_id"] for m in members}),
            "current_hashes": current_hashes,
            "members": members,
            "versions": versions,
        })

    source_local.sort(key=lambda item: (item["source_id"], item["item_key"]))
    snapshots.sort(key=lambda item: (item["normalized_url"], item["content_hash"]))
    conflict_lineages = [lineage for lineage in url_lineages
                         if lineage["has_conflicting_versions"]]
    sources_summary = [
        {
            "source_id": source_id,
            "latest_run_id": row["run_id"],
            "latest_run_status": row["status"],
            "truncated": row["truncated"],
            "observed_at": row["observed_at"],
            "latest_complete_run_id": complete_runs.get(source_id, {}).get("run_id"),
        }
        for source_id, row in sorted(latest_runs.items())
    ]
    current_clock = clock or SystemClock()
    return {
        "schema_version": GLOBAL_SCHEMA_VERSION,
        "generated_at": current_clock.now_iso(),
        "view_basis": {
            "url_lineage_view": URL_VIEW_BASIS,
            "snapshot_view": SNAPSHOT_VIEW_BASIS,
            "source_local_view": SOURCE_LOCAL_BASIS,
        },
        "stats": {
            "url_lineage_count": len(url_lineages),
            "conflicting_url_lineage_count": len(conflict_lineages),
            "content_snapshot_count": len(snapshots),
            "linked_item_count": sum(len(lineage["members"]) for lineage in url_lineages),
            "source_local_item_count": len(source_local),
            "source_count": len(latest_runs),
        },
        "sources": sources_summary,
        "url_view": url_lineages,
        "snapshot_view": snapshots,
        "source_local_items": source_local,
    }


def render_global_markdown(global_report: dict[str, Any]) -> bytes:
    lines = [
        "# feedkeep 跨源条目谱系报告（global-report，只读派生）",
        "",
        f"- 报告生成：{global_report['generated_at']}",
        "",
        "## 统计口径",
        "",
        f"- URL 谱系视图：{global_report['view_basis']['url_lineage_view']}",
        f"- 内容快照视图：{global_report['view_basis']['snapshot_view']}",
        f"- source-local：{global_report['view_basis']['source_local_view']}",
        "",
        "## 汇总",
        "",
        _line(["指标", "数值"]),
        _line(["---", "---"]),
    ]
    stat_labels = [
        ("url_lineage_count", "URL 页面谱系对象数"),
        ("conflicting_url_lineage_count", "其中存在冲突版本的谱系数"),
        ("content_snapshot_count", "内容快照对象数 (normalized_url, content_hash)"),
        ("linked_item_count", "带 link 的 source-local 条目数"),
        ("source_local_item_count", "无 link、维持 source-local 的条目数"),
        ("source_count", "有抓取记录的来源数"),
    ]
    for key, label in stat_labels:
        lines.append(_line([label, global_report["stats"][key]]))

    lines.extend(["", "## 来源最近观察状态", "",
                  _line(["来源", "最近 run", "状态", "截断", "最近完整 run", "观察时刻"]),
                  _line(["---"] * 6)])
    if global_report["sources"]:
        for source in global_report["sources"]:
            lines.append(_line([
                source["source_id"], source["latest_run_id"], source["latest_run_status"],
                source["truncated"], source["latest_complete_run_id"], source["observed_at"],
            ]))
    else:
        lines.append("| （无） | - | - | - | - | - |")

    lines.extend(["", "## URL 谱系视图（同 URL = 一个页面谱系，多 hash = 版本候选）", "",
                  _line(["规范化 URL", "谱系状态", "当前版本 hash 数", "活跃来源", "成员来源", "谱系 id"]),
                  _line(["---"] * 6)])
    if global_report["url_view"]:
        for lineage in global_report["url_view"]:
            lines.append(_line([
                lineage["normalized_url"], lineage["status"],
                len(lineage["current_hashes"]), lineage["active_source_count"],
                lineage["member_source_count"], lineage["lineage_id"],
            ]))
    else:
        lines.append("| （无带 link 的条目） | - | - | - | - | - |")

    for lineage in global_report["url_view"]:
        lines.extend(["", f"### 页面谱系：{lineage['normalized_url']}", ""])
        lines.append(f"- 状态：`{lineage['status']}`；冲突：{lineage['has_conflicting_versions']}")
        lines.append(f"- 谱系 id：`{lineage['lineage_id']}`")
        lines.append("")
        lines.append(_line([
            "版本 hash", "当前版本", "当前来源", "持有来源", "首见 run", "最近观察 run",
        ]))
        lines.append(_line(["---"] * 6))
        for version in lineage["versions"]:
            lines.append(_line([
                version["content_hash"], version["is_current"],
                ",".join(version["current_sources"]),
                ",".join(version["holder_sources"]),
                version["first_observed_run_id"], version["last_observed_run_id"],
            ]))
        lines.extend(["", "成员（保留 source-local item_key 与生命周期）：", ""])
        lines.append(_line([
            "来源", "item_key", "身份", "当前 hash", "成员状态", "当前性", "当前性依据",
            "missing_since", "最近 run", "最近完整 run",
        ]))
        lines.append(_line(["---"] * 10))
        for member in lineage["members"]:
            lines.append(_line([
                member["source_id"], member["item_key"], member["identity_kind"],
                member["content_hash"], member["state"], member["currency"],
                member["currency_reason"], member["missing_since"],
                member["last_run_id"], member["latest_complete_run_id"],
            ]))

    lines.extend(["", "## 内容快照视图（每个 (URL, hash) = 一个独立内容快照）", "",
                  _line(["快照 id", "规范化 URL", "content_hash", "当前", "成员来源",
                         "首见 run", "最近观察 run"]),
                  _line(["---"] * 7)])
    if global_report["snapshot_view"]:
        for snapshot in global_report["snapshot_view"]:
            lines.append(_line([
                snapshot["snapshot_id"], snapshot["normalized_url"],
                snapshot["content_hash"], snapshot["is_current"],
                ",".join(snapshot["member_sources"]),
                snapshot["first_observed_run_id"], snapshot["last_observed_run_id"],
            ]))
    else:
        lines.append("| （无） | - | - | - | - | - | - |")

    lines.extend(["", "## 无 link 条目（维持 source-local 身份，不做猜测式合并）", "",
                  _line(["来源", "item_key", "身份", "当前 hash", "成员状态", "当前性",
                         "missing_since", "最近 run"]),
                  _line(["---"] * 8)])
    if global_report["source_local_items"]:
        for member in global_report["source_local_items"]:
            lines.append(_line([
                member["source_id"], member["item_key"], member["identity_kind"],
                member["content_hash"], member["state"], member["currency"],
                member["missing_since"], member["last_run_id"],
            ]))
    else:
        lines.append("| （无） | - | - | - | - | - | - | - |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_global_reports(store: Store, out_dir: str | Path,
                         clock: Optional[Clock] = None
                         ) -> tuple[Path, Path, dict[str, Any]]:
    global_report = build_global_report(store, clock)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = "global-report"
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_bytes(render_json(global_report))
    markdown_path.write_bytes(render_global_markdown(global_report))
    return json_path, markdown_path, global_report
