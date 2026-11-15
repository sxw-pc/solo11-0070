from __future__ import annotations

import sqlite3

import pytest

from feedkeep.fetcher import Fetcher
from feedkeep.ledger import Ledger
from feedkeep.policy import Policy
from feedkeep.recovery import RecoveryError
from feedkeep.report import build_report


class SimulatedCrash(BaseException):
    """Stands in for the process dying between two sources."""


class CrashingFetcher:
    """Wraps a real Fetcher and dies before the Nth logical fetch."""

    def __init__(self, inner: Fetcher, crash_at: int):
        self.inner = inner
        self.crash_at = crash_at
        self.calls = 0

    def fetch(self, url, etag=None, last_modified=None):
        if self.calls == self.crash_at:
            raise SimulatedCrash(f"simulated crash before fetch #{self.crash_at}")
        self.calls += 1
        return self.inner.fetch(url, etag=etag, last_modified=last_modified)


def _crash_run(store, policy, clock, sources, crash_at, config_path="feeds.yaml"):
    ledger = Ledger(store, policy,
                    CrashingFetcher(Fetcher(policy, clock), crash_at), clock)
    with pytest.raises(SimulatedCrash):
        ledger.run(sources, config_path)
    running = store.running_run()
    assert running is not None
    return running["id"]


def _resume(store, policy, clock, run_id, sources, config_path="feeds.yaml"):
    return Ledger(store, policy, Fetcher(policy, clock), clock).resume(
        run_id, sources, config_path
    )


def _finalize(store, policy, clock, run_id, sources, config_path="feeds.yaml"):
    return Ledger(store, policy, Fetcher(policy, clock), clock).finalize(
        run_id, sources, config_path
    )


@pytest.fixture
def three_sources(mock_server, source_factory):
    return [
        source_factory(mock_server, source_id="a-rss"),
        source_factory(mock_server, path="/feeds/jsonfeed-basic.json",
                       source_id="b-json", fmt="jsonfeed"),
        source_factory(mock_server, path="/feeds/atom-mixed.xml",
                       source_id="c-atom", fmt="atom"),
    ]


def test_crash_before_first_source_resumes_from_scratch(
    store, mock_server, fixed_clock, default_policy, three_sources,
):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=0)
    assert store.get_source_runs(run_id) == []
    assert store.get_run(run_id)["status"] == "running"
    mock_server.reset()

    outcome = _resume(store, default_policy, fixed_clock, run_id, three_sources)

    assert outcome.action == "resume"
    assert outcome.run_id == run_id
    assert outcome.reused_source_ids == ()
    assert outcome.processed_source_ids == ("a-rss", "b-json", "c-atom")
    summary = outcome.summary
    assert summary.status == "ok"
    assert summary.counts.sources_total == 3
    assert summary.counts.sources_ok == 3
    assert summary.counts.items_new == 8  # 3 RSS + 3 JSON Feed + 2 Atom
    assert summary.counts.item_errors == 3
    assert store.get_run(run_id)["status"] == "ok"
    assert store.get_run(run_id)["resume_count"] == 1
    assert len({request["path"] for request in mock_server.snapshot()}) == 3


def test_partial_success_resume_skips_completed_source(
    store, mock_server, fixed_clock, default_policy, three_sources,
):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    recorded = store.get_source_run_records(run_id)
    assert [record.source_id for record in recorded] == ["a-rss"]
    assert recorded[0].items_new == 3
    first_snapshot = mock_server.snapshot()
    assert [request["path"] for request in first_snapshot] == ["/feeds/rss-basic.xml"]
    mock_server.reset()

    outcome = _resume(store, default_policy, fixed_clock, run_id, three_sources)

    paths = sorted(request["path"] for request in mock_server.snapshot())
    assert paths == ["/feeds/atom-mixed.xml", "/feeds/jsonfeed-basic.json"]
    assert all(not request["if_none_match"] for request in mock_server.snapshot())
    assert outcome.reused_source_ids == ("a-rss",)
    assert outcome.processed_source_ids == ("b-json", "c-atom")
    summary = outcome.summary
    assert summary.counts.items_new == 8
    assert summary.counts.items_seen == 8
    assert summary.counts.sources_ok == 3
    assert summary.counts.item_errors == 3
    assert len(store.get_source_runs(run_id)) == 3

    rows = store.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()
    assert rows["n"] == 8


def test_resume_after_source_failure_keeps_failed_fact(
    store, mock_server, fixed_clock, default_policy, source_factory,
):
    sources = [
        source_factory(mock_server, path="/feeds/rss-malformed.xml",
                       source_id="a-broken", fmt="rss"),
        source_factory(mock_server, source_id="b-rss"),
        source_factory(mock_server, source_id="c-rss"),
    ]
    run_id = _crash_run(store, default_policy, fixed_clock, sources, crash_at=1)
    recorded = store.get_source_run_records(run_id)
    assert [record.source_id for record in recorded] == ["a-broken"]
    assert recorded[0].status == "failed"
    assert recorded[0].error_code == "PARSE_INVALID_XML"

    outcome = _resume(store, default_policy, fixed_clock, run_id, sources)

    assert outcome.summary.status == "partial"
    assert outcome.summary.counts.sources_failed == 1
    assert outcome.summary.counts.sources_ok == 2
    assert outcome.reused_source_ids == ("a-broken",)
    assert outcome.processed_source_ids == ("b-rss", "c-rss")
    statuses = {row["source_id"]: row["status"] for row in store.get_source_runs(run_id)}
    assert statuses["a-broken"] == "failed"


def test_resume_twice_does_not_refetch_or_double_count(
    store, mock_server, fixed_clock, default_policy, three_sources,
):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    first = _resume(store, default_policy, fixed_clock, run_id, three_sources)
    assert first.summary.counts.items_new == 8
    mock_server.reset()

    with pytest.raises(RecoveryError) as excinfo:
        _resume(store, default_policy, fixed_clock, run_id, three_sources)
    assert excinfo.value.code == "RECOVERY_NOT_RUNNING"
    assert mock_server.snapshot() == []
    assert store.get_run(run_id)["items_new"] == 8
    assert store.get_run(run_id)["resume_count"] == 1


def test_changed_source_set_is_rejected_by_resume_but_finalize_is_explicit(
    store, mock_server, fixed_clock, default_policy, three_sources, source_factory,
):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    changed = list(reversed(three_sources))
    mock_server.reset()

    with pytest.raises(RecoveryError) as excinfo:
        _resume(store, default_policy, fixed_clock, run_id, changed)
    assert excinfo.value.code == "RECOVERY_PLAN_MISMATCH"
    assert store.get_run(run_id)["status"] == "running"
    assert mock_server.snapshot() == []

    outcome = _finalize(store, default_policy, fixed_clock, run_id, changed)
    assert outcome.action == "finalize"
    assert outcome.plan_changed is True
    assert outcome.interrupted_run_id == run_id
    assert outcome.run_id != run_id
    old = store.get_run(run_id)
    assert old["status"] == "interrupted"
    assert old["successor_run_id"] == outcome.run_id
    assert old["recovery_action"] == "finalize"
    assert old["items_new"] == 3
    assert old["finished_at"] is not None
    new = store.get_run(outcome.run_id)
    assert new["status"] == "ok"
    assert new["prior_run_id"] == run_id
    assert new["recovery_action"] == "finalize"
    assert new["sources_total"] == 3
    assert len(store.get_source_runs(outcome.run_id)) == 3
    # a-rss was already archived in the interrupted round, so the fresh round
    # legitimately sees a 304 while the other two sources deliver their items.
    assert outcome.summary.counts.sources_not_modified == 1
    assert outcome.summary.counts.items_new == 5


def test_policy_change_is_part_of_the_plan(store, mock_server, fixed_clock,
                                          default_policy, three_sources):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    narrower = Policy(max_items_per_feed=2, min_interval_ms=100, max_retries=0)
    with pytest.raises(RecoveryError) as excinfo:
        _resume(store, narrower, fixed_clock, run_id, three_sources)
    assert excinfo.value.code == "RECOVERY_PLAN_MISMATCH"


def test_disabled_source_does_not_change_the_plan(store, mock_server, fixed_clock,
                                                  default_policy, source_factory):
    active = source_factory(mock_server, source_id="a-rss")
    disabled = source_factory(mock_server, source_id="z-disabled", enabled=False)
    run_id = _crash_run(store, default_policy, fixed_clock, [active, disabled], crash_at=0)
    renamed = source_factory(mock_server, source_id="z-disabled", enabled=False)
    outcome = _resume(store, default_policy, fixed_clock,
                      run_id, [disabled, active, renamed])
    assert outcome.processed_source_ids == ("a-rss",)
    assert outcome.summary.counts.sources_total == 1


def test_tampered_source_record_blocks_resume(store, mock_server, fixed_clock,
                                              default_policy, three_sources):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    with store.conn:
        store.conn.execute(
            "UPDATE source_runs SET items_new=999 WHERE run_id=? AND source_id='a-rss'",
            (run_id,),
        )
    with pytest.raises(RecoveryError) as excinfo:
        _resume(store, default_policy, fixed_clock, run_id, three_sources)
    assert excinfo.value.code == "RECOVERY_SOURCE_STATE_INVALID"
    assert store.get_run(run_id)["status"] == "running"


def test_missing_digest_blocks_resume(store, mock_server, fixed_clock,
                                      default_policy, three_sources):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    with store.conn:
        store.conn.execute(
            "UPDATE source_runs SET record_digest=NULL WHERE run_id=?", (run_id,)
        )
    with pytest.raises(RecoveryError) as excinfo:
        _resume(store, default_policy, fixed_clock, run_id, three_sources)
    assert excinfo.value.code == "RECOVERY_SOURCE_STATE_INVALID"


def test_resume_requires_a_persisted_plan(store, mock_server, fixed_clock,
                                          default_policy, three_sources):
    run_id = store.create_run("2024-03-01T00:00:00Z", "feeds.yaml")  # legacy: no plan
    with pytest.raises(RecoveryError) as excinfo:
        _resume(store, default_policy, fixed_clock, run_id, three_sources)
    assert excinfo.value.code == "RECOVERY_PLAN_MISSING"
    assert mock_server.snapshot() == []

    outcome = _finalize(store, default_policy, fixed_clock, run_id, three_sources)
    assert outcome.action == "finalize"
    assert store.get_run(run_id)["status"] == "interrupted"
    assert outcome.summary.counts.sources_total == 3


def test_resume_rejects_unknown_and_finished_runs(store, mock_server, fixed_clock,
                                                  default_policy, three_sources):
    with pytest.raises(RecoveryError) as missing:
        _resume(store, default_policy, fixed_clock, "run-999999", three_sources)
    assert missing.value.code == "RECOVERY_RUN_NOT_FOUND"

    summary = Ledger(store, default_policy,
                     Fetcher(default_policy, fixed_clock), fixed_clock).run(three_sources)
    with pytest.raises(RecoveryError) as finished:
        _resume(store, default_policy, fixed_clock, summary.run_id, three_sources)
    assert finished.value.code == "RECOVERY_NOT_RUNNING"
    with pytest.raises(RecoveryError) as finalizing:
        _finalize(store, default_policy, fixed_clock, summary.run_id, three_sources)
    assert finalizing.value.code == "RECOVERY_NOT_RUNNING"


def test_resume_can_be_interrupted_again_and_finish(
    store, mock_server, fixed_clock, default_policy, three_sources,
):
    run_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    second_crash = Ledger(
        store, default_policy,
        CrashingFetcher(Fetcher(default_policy, fixed_clock), crash_at=1), fixed_clock,
    )
    with pytest.raises(SimulatedCrash):
        second_crash.resume(run_id, three_sources)
    # a-rss from the first attempt, b-json from the interrupted resume
    assert [record.source_id for record in store.get_source_run_records(run_id)] == [
        "a-rss", "b-json",
    ]
    assert store.get_run(run_id)["status"] == "running"
    mock_server.reset()

    outcome = _resume(store, default_policy, fixed_clock, run_id, three_sources)
    assert outcome.reused_source_ids == ("a-rss", "b-json")
    assert outcome.processed_source_ids == ("c-atom",)
    assert [request["path"] for request in mock_server.snapshot()] == [
        "/feeds/atom-mixed.xml"
    ]
    assert outcome.summary.counts.items_new == 8
    assert store.get_run(run_id)["resume_count"] == 2
    assert store.get_run(run_id)["status"] == "ok"


def test_crash_after_last_source_only_recomputes_summary(
    store, mock_server, fixed_clock, default_policy, three_sources, monkeypatch,
):
    def crash(*args, **kwargs):
        raise SimulatedCrash("simulated crash before the summary write")

    monkeypatch.setattr(store, "finish_run", crash)
    ledger = Ledger(store, default_policy,
                    Fetcher(default_policy, fixed_clock), fixed_clock)
    with pytest.raises(SimulatedCrash):
        ledger.run(three_sources, "feeds.yaml")
    monkeypatch.undo()

    run_id = store.running_run()["id"]
    assert len(store.get_source_runs(run_id)) == 3
    assert store.get_run(run_id)["finished_at"] is None
    assert store.get_run(run_id)["items_new"] == 0  # summary never written
    mock_server.reset()

    outcome = _resume(store, default_policy, fixed_clock, run_id, three_sources)

    assert mock_server.snapshot() == []
    assert outcome.processed_source_ids == ()
    assert outcome.reused_source_ids == ("a-rss", "b-json", "c-atom")
    assert outcome.summary.counts.items_new == 8
    assert outcome.summary.counts.item_errors == 3
    assert store.get_run(run_id)["status"] == "ok"
    assert store.get_run(run_id)["finished_at"] is not None


def test_plain_fetch_never_silently_reuses_a_running_run(
    store, mock_server, fixed_clock, default_policy, three_sources,
):
    stale_id = _crash_run(store, default_policy, fixed_clock, three_sources, crash_at=1)
    summary = Ledger(store, default_policy,
                     Fetcher(default_policy, fixed_clock), fixed_clock).run(three_sources)
    assert summary.run_id != stale_id
    assert store.get_run(stale_id)["status"] == "running"
    assert store.get_run(summary.run_id)["status"] == "ok"
    assert store.latest_run_id() == summary.run_id
    assert build_report(store, summary.run_id, fixed_clock)["run"]["run_id"] == summary.run_id


def test_version_one_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES('schema_version','1');
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, config_path TEXT,
            sources_total INTEGER NOT NULL DEFAULT 0, sources_ok INTEGER NOT NULL DEFAULT 0,
            sources_not_modified INTEGER NOT NULL DEFAULT 0,
            sources_failed INTEGER NOT NULL DEFAULT 0, items_seen INTEGER NOT NULL DEFAULT 0,
            items_new INTEGER NOT NULL DEFAULT 0, items_updated INTEGER NOT NULL DEFAULT 0,
            items_missing INTEGER NOT NULL DEFAULT 0, items_restored INTEGER NOT NULL DEFAULT 0,
            item_errors INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE source_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            source_id TEXT NOT NULL, source_name TEXT NOT NULL, status TEXT NOT NULL,
            http_status INTEGER, duration_ms INTEGER NOT NULL, body_bytes INTEGER NOT NULL,
            body_sha256 TEXT, etag TEXT, last_modified TEXT, attempts INTEGER NOT NULL,
            items_total INTEGER NOT NULL DEFAULT 0, items_new INTEGER NOT NULL DEFAULT 0,
            items_updated INTEGER NOT NULL DEFAULT 0, items_missing INTEGER NOT NULL DEFAULT 0,
            items_restored INTEGER NOT NULL DEFAULT 0, item_errors INTEGER NOT NULL DEFAULT 0,
            error_code TEXT, error_message TEXT, truncated INTEGER NOT NULL DEFAULT 0,
            observed_at TEXT NOT NULL, UNIQUE (run_id, source_id)
        );
        INSERT INTO runs(id, started_at, status) VALUES('run-000001','2024-01-01T00:00:00Z','ok');
        INSERT INTO source_runs(
            run_id, source_id, source_name, status, duration_ms, body_bytes,
            attempts, observed_at)
        VALUES('run-000001','legacy','legacy','ok',5,10,1,'2024-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    from feedkeep.store import Store

    with Store(path) as store:
        version = store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert version == "2"
        assert "record_digest" in {
            row["name"] for row in store.conn.execute("PRAGMA table_info(source_runs)")
        }
        assert "snapshot_json" in {
            row["name"] for row in store.conn.execute("PRAGMA table_info(runs)")
        }
        # the pre-existing row survives; its missing digest is rejected by resume
        with pytest.raises(RecoveryError) as excinfo:
            store.verify_recorded_source_runs("run-000001")
        assert excinfo.value.code == "RECOVERY_SOURCE_STATE_INVALID"
