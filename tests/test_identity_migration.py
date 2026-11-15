"""Identity-contract migration: planning, blocking conflicts and idempotency."""

from __future__ import annotations

import json

import pytest

from feedkeep.cli import main as cli_main
from feedkeep.identity import IDENTITY_ID_FIRST, IDENTITY_LINK_FIRST, normalize_item
from feedkeep.migration import (
    CONFLICT_LEDGER_DRIFT,
    CONFLICT_MERGE_CONTENT,
    CONFLICT_MERGE_DUPLICATE,
    CONFLICT_SPLIT,
    CONFLICT_UNKNOWN_SOURCE,
    MigrationBlocked,
    apply_migration,
    plan_migration,
)
from feedkeep.models import FeedSource, ParsedItem

OBSERVED = "2024-06-01T00:00:00Z"


def _source(source_id="s1", contract=IDENTITY_LINK_FIRST):
    return FeedSource(
        id=source_id, name=f"source {source_id}",
        url="http://127.0.0.1:8765/feeds/rss-basic.xml",
        format="auto", enabled=True, tags=("test",), note="",
        identity_contract=contract,
    )


def _parsed(raw_id, link, title="标题", published="2024-01-01T00:00:00Z", **kw):
    return ParsedItem(
        raw_id=raw_id, title=title, link=link, summary=kw.get("summary"),
        content=kw.get("content"), published_at=published,
    )


def _seed(store, source, items, observed_at="2024-01-01T00:00:00Z",
          contract=IDENTITY_ID_FIRST):
    store.sync_sources([_source(source.id, contract)])
    run_id = store.create_run(observed_at, None)
    normalized = [normalize_item(item, contract) for item in items]
    store.apply_observation(source.id, run_id, observed_at, normalized, [])
    return normalized


def _item_row(store, source_id, item_key):
    return store.conn.execute(
        "SELECT * FROM items WHERE source_id=? AND item_key=?",
        (source_id, item_key),
    ).fetchone()


def test_plan_is_read_only_and_reports_current_contract(store):
    source = _source()
    item = _parsed("urn:1", "http://127.0.0.1:8765/a")
    seeded = _seed(store, source, [item])
    before = _item_row(store, source.id, seeded[0].item_key)

    targets = {source.id: IDENTITY_LINK_FIRST}
    report = plan_migration(store, targets)
    assert not report.blocked
    plan = report.plan_for(source.id)
    assert plan.from_contract == IDENTITY_ID_FIRST
    assert plan.to_contract == IDENTITY_LINK_FIRST
    assert plan.changed_count == 1
    # Nothing was written.
    after = _item_row(store, source.id, seeded[0].item_key)
    assert dict(after) == dict(before)
    assert store.get_migration_records(source.id) == []


def test_id_rotation_stable_link_migrates_as_one_continuous_history(store):
    source = _source()
    old_item = _parsed("urn:old", "http://127.0.0.1:8765/article")
    seeded = _seed(store, source, [old_item])
    old_key = seeded[0].item_key
    assert old_key.startswith("id:")
    run2 = store.create_run("2024-02-01T00:00:00Z", None)
    # Mark missing first so we can prove missing state survives the rename.
    store.apply_observation(source.id, run2, "2024-02-01T00:00:00Z", [], [])
    missing_row = _item_row(store, source.id, old_key)
    assert missing_row["missing_since"] == "2024-02-01T00:00:00Z"
    first_seen = missing_row["first_seen_at"]
    last_changed = missing_row["last_changed_at"]

    report = apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    plan = report.plan_for(source.id)
    assert plan.changed_count == 1
    new_key = plan.mappings[0].new_key
    assert new_key.startswith("link:")

    # Old identity is gone, new identity carries the full ledger.
    assert _item_row(store, source.id, old_key) is None
    row = _item_row(store, source.id, new_key)
    assert row is not None
    assert row["first_seen_at"] == first_seen
    assert row["last_changed_at"] == last_changed
    assert row["missing_since"] == "2024-02-01T00:00:00Z"
    assert row["content_hash"] == missing_row["content_hash"]

    events = store.conn.execute(
        "SELECT event_type, item_key, detail_json FROM item_events "
        "WHERE source_id=? ORDER BY id", (source.id,)
    ).fetchall()
    types = [row["event_type"] for row in events]
    assert types == ["discovered", "missing", "identity_changed"]
    changed = events[-1]
    assert changed["item_key"] == new_key
    detail = json.loads(changed["detail_json"])
    assert detail["old_item_key"] == old_key
    assert detail["new_item_key"] == new_key
    assert detail["from_contract"] == "id_first"
    assert detail["to_contract"] == "link_first"

    ledger = store.get_migration_records(source.id)
    assert len(ledger) == 1
    assert ledger[0]["items_migrated"] == 1
    assert store.get_identity_contract(source.id) == IDENTITY_LINK_FIRST


def test_post_migration_rotated_publisher_id_matches_same_history(store):
    source = _source()
    old_item = _parsed("urn:old", "http://127.0.0.1:8765/article")
    seeded = _seed(store, source, [old_item])
    apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    new_key = normalize_item(old_item, IDENTITY_LINK_FIRST).item_key

    # Publisher rotates the id; under link_first the observation must land on
    # the existing entry instead of creating a second business item.
    rotated = _parsed("urn:brand-new", "http://127.0.0.1:8765/article")
    run = store.create_run("2024-07-01T00:00:00Z", None)
    stats = store.apply_observation(
        source.id, run, "2024-07-01T00:00:00Z",
        [normalize_item(rotated, IDENTITY_LINK_FIRST)], [],
    )
    assert stats.items_new == 0
    assert stats.items_missing == 0
    row = _item_row(store, source.id, new_key)
    assert row["raw_id"] == "urn:brand-new"
    rows = store.conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id=?", (source.id,)
    ).fetchone()
    assert rows["n"] == 1


def test_shared_link_different_ids_blocked_as_content_merge_conflict(store):
    source = _source()
    first = _parsed("urn:1", "http://127.0.0.1:8765/shared", content="body-a")
    second = _parsed("urn:2", "http://127.0.0.1:8765/shared", content="body-b")
    seeded = _seed(store, source, [first, second])
    old_keys = sorted(item.item_key for item in seeded)

    report = plan_migration(store, {source.id: IDENTITY_LINK_FIRST})
    assert report.blocked
    conflict = report.conflicts[0]
    assert conflict.code == CONFLICT_MERGE_CONTENT
    assert conflict.source_id == source.id
    assert conflict.old_keys == tuple(old_keys)
    assert len(conflict.new_keys) == 1
    assert "内容" in conflict.reason

    with pytest.raises(MigrationBlocked):
        apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    # Nothing was renamed: both old identities and their content remain.
    for key in old_keys:
        assert _item_row(store, source.id, key) is not None
    assert store.get_migration_records(source.id) == []


def test_shared_link_duplicate_content_is_still_blocked_not_auto_merged(store):
    source = _source()
    first = _parsed("urn:1", "http://127.0.0.1:8765/shared", content="same")
    second = _parsed("urn:2", "http://127.0.0.1:8765/shared", content="same")
    _seed(store, source, [first, second])

    report = plan_migration(store, {source.id: IDENTITY_LINK_FIRST})
    assert report.blocked
    assert report.conflicts[0].code == CONFLICT_MERGE_DUPLICATE
    with pytest.raises(MigrationBlocked):
        apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)


def test_one_old_to_many_new_split_is_blocked(store):
    source = _source()
    item = _parsed("urn:1", "http://127.0.0.1:8765/a")
    seeded = _seed(store, source, [item])
    old_key = seeded[0].item_key
    # Simulate ledger drift: a second provenance row whose stored id-key is
    # distinct but whose link-first target collides with the first row's.
    run = store.create_run("2024-02-01T00:00:00Z", None)
    forged = normalize_item(
        _parsed("urn:2", "http://127.0.0.1:8765/a", content="different body"),
        IDENTITY_ID_FIRST,
    )
    store.apply_observation(source.id, run, "2024-02-01T00:00:00Z", [forged], [])
    assert _item_row(store, source.id, old_key) is not None
    assert _item_row(store, source.id, forged.item_key) is not None

    report = plan_migration(store, {source.id: IDENTITY_LINK_FIRST})
    # Two distinct old identities collapse onto one new key with differing
    # content; planning must name both keys instead of picking a winner.
    assert report.blocked
    conflict = report.conflicts[0]
    assert conflict.code in (CONFLICT_MERGE_CONTENT, CONFLICT_SPLIT)
    assert old_key in conflict.old_keys
    assert forged.item_key in conflict.old_keys
    with pytest.raises(MigrationBlocked):
        apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    # Both originals survive untouched.
    assert _item_row(store, source.id, old_key) is not None
    assert _item_row(store, source.id, forged.item_key) is not None


def test_title_date_rows_keep_identity_when_both_primary_fields_absent(store):
    source = _source()
    bare = _parsed(None, None, title="无链接无 ID", published="2024-03-03T00:00:00Z")
    seeded = _seed(store, source, [bare])
    key = seeded[0].item_key
    assert key.startswith("td:")

    report = apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    plan = report.plan_for(source.id)
    # title+date fallback is contract-independent: the key is unchanged.
    assert plan.changed_count == 0
    assert _item_row(store, source.id, key) is not None
    # No identity_changed event is emitted for an unchanged key.
    events = store.conn.execute(
        "SELECT event_type FROM item_events WHERE source_id=?", (source.id,)
    ).fetchall()
    assert [row["event_type"] for row in events] == ["discovered"]


def test_migration_is_idempotent_after_apply_and_interruption(store):
    source = _source()
    items = [
        _parsed("urn:1", "http://127.0.0.1:8765/a"),
        _parsed("urn:2", "http://127.0.0.1:8765/b"),
    ]
    _seed(store, source, items)

    first = apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    assert first.items_to_migrate == 2
    item_count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id=?", (source.id,)
    ).fetchone()["n"]
    event_count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM item_events WHERE source_id=? "
        "AND event_type='identity_changed'", (source.id,)
    ).fetchone()["n"]
    assert event_count == 2

    # Re-running after completion (or an interruption) must be a no-op.
    second = apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    plan = second.plan_for(source.id)
    assert plan.aligned and plan.changed_count == 0
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id=?", (source.id,)
    ).fetchone()["n"] == item_count
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM item_events WHERE source_id=? "
        "AND event_type='identity_changed'", (source.id,)
    ).fetchone()["n"] == event_count
    assert len(store.get_migration_records(source.id)) == 1

    # Switching back to id_first restores the original keys one-to-one.
    back = apply_migration(store, {source.id: IDENTITY_ID_FIRST}, "2024-08-01T00:00:00Z")
    assert back.plan_for(source.id).changed_count == 2
    assert store.get_identity_contract(source.id) == IDENTITY_ID_FIRST


def test_swap_mapping_renames_both_keys_without_collision(store):
    # A genuine swap: two stored rows exchange final keys.  This cannot be
    # produced by a single contract recomputation, so it is injected at the
    # storage boundary to prove the two-phase rename handles cycles safely.
    source = _source()
    a = _parsed("urn:a", "http://127.0.0.1:8765/a")
    b = _parsed("urn:b", "http://127.0.0.1:8765/b")
    seeded = _seed(store, source, [a, b])
    old_a, old_b = sorted(item.item_key for item in seeded)
    from feedkeep.migration import IdentityMapping
    # old_a takes old_b's key and vice versa.
    mappings = [
        IdentityMapping(old_a, old_b, "link"),
        IdentityMapping(old_b, old_a, "link"),
    ]
    store.apply_identity_migration(
        source.id, mappings, IDENTITY_ID_FIRST, IDENTITY_LINK_FIRST, OBSERVED,
    )
    keys = {
        row["item_key"]
        for row in store.conn.execute(
            "SELECT item_key FROM items WHERE source_id=?", (source.id,)
        ).fetchall()
    }
    assert keys == {old_a, old_b}
    # Every item kept exactly one identity_changed event on its new key.
    changed = store.conn.execute(
        "SELECT item_key FROM item_events WHERE source_id=? "
        "AND event_type='identity_changed' ORDER BY item_key",
        (source.id,),
    ).fetchall()
    assert [row["item_key"] for row in changed] == sorted(keys)
    # Enclosures moved with the keys (no orphan rows under temporary names).
    leftovers = store.conn.execute(
        "SELECT COUNT(*) AS n FROM item_events WHERE item_key LIKE '__migrate_%'"
    ).fetchone()["n"]
    assert leftovers == 0


def test_ledger_drift_conflict_is_reported(store):
    source = _source()
    item = _parsed("urn:1", "http://127.0.0.1:8765/a")
    _seed(store, source, [item])
    apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)
    # Manufacture an out-of-band contradiction: the sources row claims
    # id_first while the migration ledger still ends on link_first.
    with store.conn:
        store.conn.execute(
            "UPDATE sources SET identity_contract='id_first' WHERE id=?",
            (source.id,),
        )
    drifted = plan_migration(store, {source.id: IDENTITY_LINK_FIRST})
    codes = {conflict.code for conflict in drifted.conflicts}
    assert CONFLICT_LEDGER_DRIFT in codes
    with pytest.raises(MigrationBlocked):
        apply_migration(store, {source.id: IDENTITY_LINK_FIRST}, OBSERVED)


def test_unknown_source_is_reported_not_crashed(store):
    report = plan_migration(store, {"ghost": IDENTITY_LINK_FIRST})
    assert report.blocked
    conflict = report.conflicts[0]
    assert conflict.code == CONFLICT_UNKNOWN_SOURCE
    assert conflict.source_id == "ghost"
    with pytest.raises(MigrationBlocked):
        apply_migration(store, {"ghost": IDENTITY_LINK_FIRST}, OBSERVED)


def test_cli_plan_and_migrate_roundtrip(store, tmp_path, monkeypatch):
    source = _source()
    item = _parsed("urn:1", "http://127.0.0.1:8765/a")
    _seed(store, source, [item])
    db_path = store.path
    config_path = tmp_path / "feeds.yaml"
    policy_path = tmp_path / "policy.yaml"
    config_path.write_text(
        "version: 1\nfeeds:\n"
        f"  - id: {source.id}\n    name: s\n    url: http://127.0.0.1:8765/feed\n"
        "    format: auto\n    enabled: true\n    tags: []\n    note: ''\n"
        "    identity_contract: link_first\n",
        encoding="utf-8",
    )
    policy_path.write_text(
        "version: 1\nallowed_hosts: [127.0.0.1]\n", encoding="utf-8",
    )
    common = ["--config", str(config_path), "--policy", str(policy_path),
              "--db", db_path]

    rc = cli_main(["identity-plan", *common, "--json"])
    assert rc == 0
    rc = cli_main(["identity-migrate", *common])
    assert rc == 0
    # Second plan run reports alignment and exits zero.
    rc = cli_main(["identity-plan", *common])
    assert rc == 0
    assert store.get_identity_contract(source.id) == IDENTITY_LINK_FIRST

    # A conflicting source makes plan exit 4 without touching the database.
    conflict_source = _source("s2")
    store.sync_sources([_source("s2", IDENTITY_ID_FIRST)])
    run = store.create_run("2024-09-01T00:00:00Z", None)
    dupes = [
        normalize_item(_parsed("urn:x", "http://127.0.0.1:8765/shared", content="x")),
        normalize_item(_parsed("urn:y", "http://127.0.0.1:8765/shared", content="y")),
    ]
    store.apply_observation("s2", run, "2024-09-01T00:00:00Z", dupes, [])
    config_path.write_text(
        config_path.read_text(encoding="utf-8") +
        "  - id: s2\n    name: s2\n    url: http://127.0.0.1:8765/feed2\n"
        "    format: auto\n    enabled: true\n    tags: []\n    note: ''\n"
        "    identity_contract: link_first\n",
        encoding="utf-8",
    )
    rc = cli_main(["identity-plan", *common, "--json"])
    assert rc == 4
    rc = cli_main(["identity-migrate", *common])
    assert rc == 4
    # The already-migrated source stays aligned; the blocked source is intact.
    assert store.get_identity_contract("s2") == IDENTITY_ID_FIRST
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id='s2'"
    ).fetchone()["n"] == 2
