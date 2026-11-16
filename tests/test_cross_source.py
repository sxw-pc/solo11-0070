"""跨源条目谱系（global-report）测试。

覆盖验收点：
① source 配置顺序颠倒后报告字节稳定；
② 同 URL 异 hash 同时呈现为一个 URL 谱系下的冲突版本及两个内容快照；
③ 同 URL 同 hash 共享快照但仍列全来源；
④ 一个源失败 / 304 / 截断时 last-known membership 标为旧 / 未知，不被另一源的 missing 删除；
⑤ 一个源完整 missing 后，其他仍 active 的来源继续支撑 URL 谱系；
⑥ 无 link 条目不发生猜测式跨源合并。
"""

from __future__ import annotations

import json

import pytest

from feedkeep.clock import FixedClock
from feedkeep.cli import main
from feedkeep.models import FeedSource, SourceRunRecord
from feedkeep.report import (
    GLOBAL_SCHEMA_VERSION,
    ReportError,
    build_global_report,
    render_global_markdown,
    render_json,
    write_global_reports,
)
from feedkeep.store import Store

URL = "https://example.test/story/1"


def _source(source_id: str) -> FeedSource:
    return FeedSource(
        id=source_id, name=f"Source {source_id}",
        url=f"http://127.0.0.1:8765/feeds/{source_id}.xml",
        format="auto", enabled=True, tags=("test",), note="",
    )


def _source_run_record(source_id: str, run_id: str, observed_at: str,
                       status: str = "ok", truncated: bool = False) -> SourceRunRecord:
    return SourceRunRecord(
        source_id=source_id, source_name=f"Source {source_id}", status=status,
        http_status=304 if status == "not_modified" else (500 if status == "failed" else 200),
        duration_ms=10, body_bytes=0 if status != "ok" else 128, body_sha256=None,
        etag=None, last_modified=None, attempts=1, items_total=0, items_new=0,
        items_updated=0, items_missing=0, items_restored=0, item_errors=0,
        error_code="ITEMS_TRUNCATED" if truncated else None,
        error_message="仅处理前 N 条" if truncated else None,
        truncated=truncated, observed_at=observed_at,
    )


def _record_source_run(store, source_id, run_id, observed_at, **kwargs):
    store.sync_sources([_source(source_id)])
    store.add_source_run(
        run_id, _source_run_record(source_id, run_id, observed_at, **kwargs)
    )


def _make_run(store, started="2024-01-01T00:00:00Z"):
    return store.create_run(started, None)


def _finish(store, run_id, finished="2024-01-01T01:00:00Z"):
    from feedkeep.models import RunCounts

    store.finish_run(run_id, finished, "ok", RunCounts())


def _item(item_factory, raw_id, content, link=URL, **kwargs):
    return item_factory(raw_id=raw_id, content=content, link=link, **kwargs)


def _lineage(report, url=URL):
    lineages = [item for item in report["url_view"] if item["normalized_url"] == url]
    assert len(lineages) == 1
    return lineages[0]


def _snapshot_hashes(report, url=URL):
    return sorted(
        item["content_hash"] for item in report["snapshot_view"]
        if item["normalized_url"] == url
    )


# ---------------------------------------------------------------------------
# H1：同 URL 异 hash，两种遍历顺序，输出字节稳定
# ---------------------------------------------------------------------------

def _build_conflicting_archive(store, item_factory, order):
    run_id = _make_run(store)
    observed = "2024-03-01T00:00:00Z"
    for source_id, item in order:
        _record_source_run(store, source_id, run_id, observed)
        store.apply_observation(source_id, run_id, observed, [item], [])
    _finish(store, run_id)
    return order[0][1], order[1][1]


def test_same_url_different_hash_is_conflict_and_two_snapshots(store, item_factory):
    item_a = _item(item_factory, "a", "v1 body")
    item_b = _item(item_factory, "b", "v2 body")
    _build_conflicting_archive(
        store, item_factory, [("feed-a", item_a), ("feed-b", item_b)],
    )

    report = build_global_report(store)
    assert report["schema_version"] == GLOBAL_SCHEMA_VERSION

    lineage = _lineage(report)
    assert lineage["status"] == "conflict"
    assert lineage["has_conflicting_versions"] is True
    assert lineage["active_source_count"] == 2
    assert lineage["member_source_count"] == 2
    assert sorted(lineage["current_hashes"]) == sorted(
        [item_a.content_hash, item_b.content_hash]
    )
    # 两个竞争版本并列，没有胜者字段。
    assert len(lineage["versions"]) == 2
    for version in lineage["versions"]:
        assert version["is_current"] is True
        assert len(version["current_sources"]) == 1
    assert "winner" not in json.dumps(report, ensure_ascii=False)

    # 快照视图：同 URL 两个独立快照，各自保留来源成员。
    assert _snapshot_hashes(report) == sorted(
        [item_a.content_hash, item_b.content_hash]
    )
    assert report["stats"]["url_lineage_count"] == 1
    assert report["stats"]["conflicting_url_lineage_count"] == 1
    assert report["stats"]["content_snapshot_count"] == 2

    # 两个口径在报告里各自显式标注，不被合并成一个通用去重数。
    basis = report["view_basis"]
    assert "页面谱系" in basis["url_lineage_view"]
    assert "独立内容快照" in basis["snapshot_view"]
    assert "不裁决胜者" in basis["url_lineage_view"]


def test_reversed_source_order_produces_byte_identical_report(store, item_factory,
                                                              tmp_path):
    def build(order):
        from feedkeep.store import Store as _Store

        db = tmp_path / f"archive-{order[0][0]}.sqlite3"
        local = _Store(db)
        try:
            _build_conflicting_archive(
                local, item_factory,
                [(sid, _item(item_factory, rid, body)) for sid, rid, body in order],
            )
            payload = build_global_report(local, clock=FixedClock("2024-05-01T00:00:00Z"))
            return render_json(payload), render_global_markdown(payload)
        finally:
            local.close()

    order_ab = [("feed-a", "a", "v1 body"), ("feed-b", "b", "v2 body")]
    order_ba = [("feed-b", "b", "v2 body"), ("feed-a", "a", "v1 body")]
    json_ab, md_ab = build(order_ab)
    json_ba, md_ba = build(order_ba)
    assert json_ab == json_ba
    assert md_ab == md_ba

    parsed = json.loads(json_ab)
    assert [m["source_id"] for m in _lineage(parsed)["members"]] == ["feed-a", "feed-b"]


# ---------------------------------------------------------------------------
# 同 URL 同 hash：共享一个快照，但列全来源
# ---------------------------------------------------------------------------

def test_same_url_same_hash_shares_snapshot_and_lists_all_sources(
    store, item_factory,
):
    item_a = _item(item_factory, "a", "same body")
    item_b = _item(item_factory, "b", "same body")
    assert item_a.content_hash == item_b.content_hash
    run_id = _make_run(store)
    observed = "2024-03-01T00:00:00Z"
    for source_id, item in (("feed-a", item_a), ("feed-b", item_b)):
        _record_source_run(store, source_id, run_id, observed)
        store.apply_observation(source_id, run_id, observed, [item], [])
    _finish(store, run_id)

    report = build_global_report(store)
    lineage = _lineage(report)
    assert lineage["status"] == "consistent"
    assert lineage["has_conflicting_versions"] is False
    assert lineage["member_source_count"] == 2
    assert len(lineage["versions"]) == 1
    assert lineage["versions"][0]["holder_sources"] == ["feed-a", "feed-b"]
    assert lineage["versions"][0]["current_sources"] == ["feed-a", "feed-b"]

    assert report["stats"]["url_lineage_count"] == 1
    assert report["stats"]["content_snapshot_count"] == 1
    snapshots = [s for s in report["snapshot_view"] if s["normalized_url"] == URL]
    assert len(snapshots) == 1
    assert snapshots[0]["member_sources"] == ["feed-a", "feed-b"]
    assert len(snapshots[0]["members"]) == 2
    assert {m["item_key"] for m in snapshots[0]["members"]} == {
        item_a.item_key, item_b.item_key,
    }


# ---------------------------------------------------------------------------
# ④ 失败 / 304 / 截断：last-known membership 保留，不被另一源 missing 删除
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("degraded_status,truncated,expected_currency,reason_part", [
    ("failed", False, "stale", "失败"),
    ("not_modified", False, "stale", "304"),
    ("ok", True, "stale", "截断"),
])
def test_degraded_source_keeps_last_known_membership(
    store, item_factory, degraded_status, truncated, expected_currency, reason_part,
):
    item_a = _item(item_factory, "a", "v1 body")
    item_b = _item(item_factory, "b", "v2 body")

    # 第一轮：两个源都完整成功，各自发布同 URL 条目。
    run1 = _make_run(store, "2024-02-01T00:00:00Z")
    at1 = "2024-02-01T00:00:00Z"
    _record_source_run(store, "feed-a", run1, at1)
    store.apply_observation("feed-a", run1, at1, [item_a], [])
    _record_source_run(store, "feed-b", run1, at1)
    store.apply_observation("feed-b", run1, at1, [item_b], [])
    _finish(store, run1, "2024-02-01T01:00:00Z")

    # 第二轮：A 降级（失败/304/截断），B 完整成功且其条目明确缺席。
    run2 = _make_run(store, "2024-03-01T00:00:00Z")
    at2 = "2024-03-01T00:00:00Z"
    _record_source_run(
        store, "feed-a", run2, at2, status=degraded_status, truncated=truncated,
    )
    if degraded_status == "ok" and truncated:
        # 截断轮仍可能写入部分条目，但不做缺失对账。
        store.apply_observation("feed-a", run2, at2, [item_a], [], truncated=True)
    _record_source_run(store, "feed-b", run2, at2)
    store.apply_observation("feed-b", run2, at2, [], [])
    _finish(store, run2, "2024-03-01T01:00:00Z")

    report = build_global_report(store)
    lineage = _lineage(report)
    members = {m["source_id"]: m for m in lineage["members"]}

    # A 的 last-known 关联仍在，状态 active、当前性降级，未被 B 的 missing 连坐。
    assert members["feed-a"]["state"] == "active"
    assert members["feed-a"]["currency"] == expected_currency
    assert reason_part in members["feed-a"]["currency_reason"]
    assert members["feed-a"]["missing_since"] is None
    assert members["feed-a"]["latest_source_run_id"] == run2

    # B 在完整成功轮中明确缺席，成员关联结束。
    assert members["feed-b"]["state"] == "missing"
    assert members["feed-b"]["currency"] == "ended"
    assert members["feed-b"]["missing_since"] == at2

    # A 单独继续支撑该 URL 谱系；其持有版本仍是当前版本。
    assert lineage["active_source_count"] == 1
    assert lineage["current_hashes"] == [item_a.content_hash]
    assert lineage["status"] == "consistent"

    # 最近完整 run 停留在 run1；run2 的降级状态不充当完整观察。
    assert members["feed-a"]["latest_complete_run_id"] == run1
    source_summary = {s["source_id"]: s for s in report["sources"]}
    assert source_summary["feed-a"]["latest_run_id"] == run2
    assert source_summary["feed-a"]["latest_complete_run_id"] == run1
    assert source_summary["feed-b"]["latest_complete_run_id"] == run2


def test_failed_source_is_not_treated_as_deletion(store, item_factory):
    item_a = _item(item_factory, "a", "v1 body")
    run1 = _make_run(store, "2024-02-01T00:00:00Z")
    at1 = "2024-02-01T00:00:00Z"
    _record_source_run(store, "feed-a", run1, at1)
    store.apply_observation("feed-a", run1, at1, [item_a], [])
    _finish(store, run1, "2024-02-01T01:00:00Z")

    run2 = _make_run(store, "2024-03-01T00:00:00Z")
    at2 = "2024-03-01T00:00:00Z"
    _record_source_run(store, "feed-a", run2, at2, status="failed")
    _finish(store, run2, "2024-03-01T01:00:00Z")

    report = build_global_report(store)
    member = _lineage(report)["members"][0]
    assert member["state"] == "active"
    assert member["currency"] == "stale"
    assert member["missing_since"] is None
    # items 行本身没有被失败轮改写。
    row = store.conn.execute(
        "SELECT missing_since,last_run_id FROM items WHERE source_id='feed-a'"
    ).fetchone()
    assert row["missing_since"] is None
    assert row["last_run_id"] == run1


# ---------------------------------------------------------------------------
# ⑤ 一个源完整 missing，其他 active 源继续支撑谱系
# ---------------------------------------------------------------------------

def test_missing_in_one_source_does_not_close_lineage(store, item_factory):
    item_a = _item(item_factory, "a", "v1 body")
    item_b = _item(item_factory, "b", "v1 body")
    run1 = _make_run(store, "2024-02-01T00:00:00Z")
    at1 = "2024-02-01T00:00:00Z"
    _record_source_run(store, "feed-a", run1, at1)
    store.apply_observation("feed-a", run1, at1, [item_a], [])
    _record_source_run(store, "feed-b", run1, at1)
    store.apply_observation("feed-b", run1, at1, [item_b], [])
    _finish(store, run1, "2024-02-01T01:00:00Z")

    run2 = _make_run(store, "2024-03-01T00:00:00Z")
    at2 = "2024-03-01T00:00:00Z"
    # B 完整成功但条目缺席；A 完整成功且条目仍在。
    _record_source_run(store, "feed-a", run2, at2)
    store.apply_observation("feed-a", run2, at2, [item_a], [])
    _record_source_run(store, "feed-b", run2, at2)
    store.apply_observation("feed-b", run2, at2, [], [])
    _finish(store, run2, "2024-03-01T01:00:00Z")

    report = build_global_report(store)
    lineage = _lineage(report)
    assert lineage["status"] != "inactive"
    assert lineage["active_source_count"] == 1
    members = {m["source_id"]: m for m in lineage["members"]}
    assert members["feed-a"]["state"] == "active"
    assert members["feed-a"]["currency"] == "current"
    assert members["feed-b"]["state"] == "missing"
    # 历史版本仍可在版本候选中追溯到 B。
    version = lineage["versions"][0]
    assert version["holder_sources"] == ["feed-a", "feed-b"]
    assert version["current_sources"] == ["feed-a"]


def test_lineage_goes_inactive_only_when_all_sources_missing(store, item_factory):
    item_a = _item(item_factory, "a", "v1 body")
    run1 = _make_run(store, "2024-02-01T00:00:00Z")
    at1 = "2024-02-01T00:00:00Z"
    _record_source_run(store, "feed-a", run1, at1)
    store.apply_observation("feed-a", run1, at1, [item_a], [])
    _finish(store, run1, "2024-02-01T01:00:00Z")

    run2 = _make_run(store, "2024-03-01T00:00:00Z")
    at2 = "2024-03-01T00:00:00Z"
    _record_source_run(store, "feed-a", run2, at2)
    store.apply_observation("feed-a", run2, at2, [], [])
    _finish(store, run2, "2024-03-01T01:00:00Z")

    report = build_global_report(store)
    lineage = _lineage(report)
    assert lineage["status"] == "inactive"
    assert lineage["active_source_count"] == 0
    assert lineage["current_hashes"] == []
    # 历史快照仍保留，只是不再 current。
    assert lineage["versions"][0]["is_current"] is False


# ---------------------------------------------------------------------------
# ⑥ 无 link 条目不猜测合并
# ---------------------------------------------------------------------------

def test_items_without_link_stay_source_local(store, item_factory):
    linked = _item(item_factory, "a", "shared body", link=URL)
    # 发布方 id 不同、无 link，仅标题相似也绝不合并。
    no_link_1 = item_factory(raw_id="x-1", title="相同标题", content="shared body",
                             link=None)
    no_link_2 = item_factory(raw_id="x-2", title="相同标题", content="shared body",
                             link=None)
    run_id = _make_run(store)
    at = "2024-03-01T00:00:00Z"
    _record_source_run(store, "feed-a", run_id, at)
    store.apply_observation("feed-a", run_id, at, [linked, no_link_1], [])
    _record_source_run(store, "feed-b", run_id, at)
    store.apply_observation("feed-b", run_id, at, [no_link_2], [])
    _finish(store, run_id)

    report = build_global_report(store)
    lineage = _lineage(report)
    assert lineage["member_source_count"] == 1
    assert [m["source_id"] for m in lineage["members"]] == ["feed-a"]

    local = report["source_local_items"]
    assert len(local) == 2
    assert {(m["source_id"], m["item_key"]) for m in local} == {
        ("feed-a", no_link_1.item_key), ("feed-b", no_link_2.item_key),
    }
    assert all(m["link"] is None for m in local) is True
    assert report["stats"]["source_local_item_count"] == 2
    # 无 link 条目不出现在任何快照里（快照键含 normalized_url，无 link 即无键）。
    snapshot_member_keys = {
        (m["source_id"], m["item_key"])
        for snapshot in report["snapshot_view"]
        for m in snapshot["members"]
    }
    assert ("feed-a", no_link_1.item_key) not in snapshot_member_keys
    assert ("feed-b", no_link_2.item_key) not in snapshot_member_keys


# ---------------------------------------------------------------------------
# 历史 hash：版本候选保留当前与历史 hash，且有 run 证据可回指
# ---------------------------------------------------------------------------

def test_hash_history_tracks_versions_with_run_evidence(store, item_factory):
    v1 = _item(item_factory, "a", "version one")
    run1 = _make_run(store, "2024-01-01T00:00:00Z")
    at1 = "2024-01-01T00:00:00Z"
    _record_source_run(store, "feed-a", run1, at1)
    store.apply_observation("feed-a", run1, at1, [v1], [])
    _finish(store, run1, "2024-01-01T01:00:00Z")

    v2 = _item(item_factory, "a", "version two")
    run2 = _make_run(store, "2024-02-01T00:00:00Z")
    at2 = "2024-02-01T00:00:00Z"
    _record_source_run(store, "feed-a", run2, at2)
    store.apply_observation("feed-a", run2, at2, [v2], [])
    _finish(store, run2, "2024-02-01T01:00:00Z")

    report = build_global_report(store)
    lineage = _lineage(report)
    assert len(lineage["versions"]) == 2
    by_hash = {v["content_hash"]: v for v in lineage["versions"]}
    old = by_hash[v1.content_hash]
    new = by_hash[v2.content_hash]
    assert old["is_current"] is False
    assert old["first_observed_run_id"] == run1
    assert old["last_observed_run_id"] == run1
    assert new["is_current"] is True
    assert new["first_observed_run_id"] == run2

    member = lineage["members"][0]
    history_hashes = [entry["content_hash"] for entry in member["hash_history"]]
    assert history_hashes == [v1.content_hash, v2.content_hash]
    assert member["hash_history"][0]["first_observed_run_id"] == run1

    # 两个历史 hash 各对应一个快照，共两个内容快照。
    assert _snapshot_hashes(report) == sorted([v1.content_hash, v2.content_hash])


# ---------------------------------------------------------------------------
# 既有 source-local 账本与事件语义不变
# ---------------------------------------------------------------------------

def test_existing_items_primary_key_and_events_unchanged(store, item_factory):
    item_a = _item(item_factory, "a", "v1 body")
    item_b = _item(item_factory, "b", "v2 body")
    run_id = _make_run(store)
    at = "2024-03-01T00:00:00Z"
    _record_source_run(store, "feed-a", run_id, at)
    store.apply_observation("feed-a", run_id, at, [item_a], [])
    _record_source_run(store, "feed-b", run_id, at)
    store.apply_observation("feed-b", run_id, at, [item_b], [])
    _finish(store, run_id)

    build_global_report(store)  # 只读派生，不写库。
    rows = store.conn.execute(
        "SELECT source_id,item_key,content_hash FROM items ORDER BY source_id,item_key"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("feed-a", item_a.item_key, item_a.content_hash),
        ("feed-b", item_b.item_key, item_b.content_hash),
    ]
    events = store.get_events(run_id)
    assert [e["event_type"] for e in events] == ["discovered", "discovered"]


def test_global_report_requires_completed_run(store):
    with pytest.raises(ReportError):
        build_global_report(store)


def test_legacy_v1_database_gains_link_index_without_touching_rows(
    tmp_path, item_factory,
):
    import sqlite3

    from feedkeep.schema import SCHEMA_SQL

    legacy_db = tmp_path / "legacy.sqlite3"
    # 手工构造一个没有 idx_items_link 的旧版 v1 库（模拟升级前的归档）。
    legacy_sql = SCHEMA_SQL.replace(
        "CREATE INDEX IF NOT EXISTS idx_items_link ON items(link);\n", ""
    )
    conn = sqlite3.connect(legacy_db)
    conn.executescript(legacy_sql)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')"
    )
    conn.commit()
    conn.close()

    # 打开旧库时幂等补齐索引，主键与版本号不变。
    reopened = Store(legacy_db)
    indexes = {
        row["name"] for row in reopened.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_items_link" in indexes
    version = reopened.conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert version == "1"

    # 旧库中的 source-local 行在跨源视图里照常工作，未被重写。
    item = _item(item_factory, "a", "v1 body")
    run_id = reopened.create_run("2024-03-01T00:00:00Z", None)
    _record_source_run(reopened, "feed-a", run_id, "2024-03-01T00:00:00Z")
    reopened.apply_observation("feed-a", run_id, "2024-03-01T00:00:00Z", [item], [])
    from feedkeep.models import RunCounts
    reopened.finish_run(run_id, "2024-03-01T01:00:00Z", "ok", RunCounts())
    report = build_global_report(reopened)
    assert report["stats"]["url_lineage_count"] == 1
    pk = reopened.conn.execute(
        "SELECT source_id,item_key FROM items"
    ).fetchall()
    assert [tuple(r) for r in pk] == [("feed-a", item.item_key)]
    reopened.close()


def test_write_global_reports_and_cli(store, item_factory, tmp_path, capsys):
    item_a = _item(item_factory, "a", "v1 body")
    run_id = _make_run(store)
    at = "2024-03-01T00:00:00Z"
    _record_source_run(store, "feed-a", run_id, at)
    store.apply_observation("feed-a", run_id, at, [item_a], [])
    _finish(store, run_id)

    out_dir = tmp_path / "global"
    json_path, md_path, report = write_global_reports(store, out_dir)
    assert json_path.name == "global-report.json"
    assert md_path.name == "global-report.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["stats"]["url_lineage_count"] == 1
    md_bytes = md_path.read_bytes()
    assert "URL 谱系视图".encode("utf-8") in md_bytes
    assert "内容快照视图".encode("utf-8") in md_bytes
    # 固定时钟下字节稳定。
    fixed = FixedClock()
    assert render_json(build_global_report(store, clock=fixed)) == render_json(
        build_global_report(store, clock=FixedClock())
    )

    code = main(["global-report", "--db", store.path, "--out", str(tmp_path / "cli")])
    assert code == 0
    out = capsys.readouterr().out
    assert "global-report.json" in out
    assert "url_lineages=1" in out

    missing_db = tmp_path / "nope.sqlite3"
    assert main(["global-report", "--db", str(missing_db)]) == 2
