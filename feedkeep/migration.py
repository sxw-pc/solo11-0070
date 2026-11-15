"""Explicit identity-contract migration: read-only planning and safe apply.

Two complete identity routes are supported per source:

* ``id_first``  — publisher id wins; two different ids under one canonical
  link stay two distinct business entries;
* ``link_first`` — canonical link wins; an article keeps one continuous
  history when the publisher rotates its id.

Switching contract changes how ``item_key`` is derived, so the existing
ledger has to be rewritten rather than left to accumulate orphaned keys.
Planning is a pure, read-only operation that recomputes every key from the
stored raw material and rejects any mapping that is not an unambiguous
one-to-one rename.  Only a conflict-free plan may be applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .identity import (
    IDENTITY_DEFAULT,
    IdentityError,
    identify_item,
    validate_contract,
)
from .models import ParsedItem
from .store import Store

#: One old identity maps onto more than one new identity.
CONFLICT_SPLIT = "IDM_ONE_OLD_MANY_NEW"
#: Several old identities map onto the same new identity with differing
#: content — merging would silently pick a winner and lose material.
CONFLICT_MERGE_CONTENT = "IDM_MANY_OLD_ONE_NEW_CONTENT"
#: Several old identities map onto the same new identity.  Even with equal
#: content fingerprints there is no provenance-safe automatic merge.
CONFLICT_MERGE_DUPLICATE = "IDM_MANY_OLD_ONE_NEW"
#: The migration ledger contradicts the keys actually stored; a previous
#: run was interrupted outside a transaction or the file was edited.
CONFLICT_LEDGER_DRIFT = "IDM_LEDGER_DRIFT"
#: The source is not present in the archive (never fetched / wrong id).
CONFLICT_UNKNOWN_SOURCE = "IDM_UNKNOWN_SOURCE"


@dataclass(frozen=True)
class IdentityMapping:
    """A single old-key -> new-key renaming decision."""

    old_key: str
    new_key: str
    kind: str

    @property
    def changed(self) -> bool:
        return self.old_key != self.new_key

    def as_dict(self) -> dict[str, Any]:
        return {"old_item_key": self.old_key, "new_item_key": self.new_key,
                "identity_kind": self.kind, "changed": self.changed}


@dataclass(frozen=True)
class MigrationConflict:
    """A blocking ambiguity with source, the involved keys and the reason."""

    source_id: str
    code: str
    old_keys: tuple[str, ...]
    new_keys: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "code": self.code,
            "old_item_keys": list(self.old_keys),
            "new_item_keys": list(self.new_keys),
            "reason": self.reason,
        }


@dataclass
class SourceMigrationPlan:
    source_id: str
    from_contract: str
    to_contract: str
    mappings: list[IdentityMapping] = field(default_factory=list)
    conflicts: list[MigrationConflict] = field(default_factory=list)

    @property
    def aligned(self) -> bool:
        """True when the stored ledger already follows the target contract."""

        return self.from_contract == self.to_contract

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    @property
    def changed_count(self) -> int:
        return sum(1 for mapping in self.mappings if mapping.changed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "from_contract": self.from_contract,
            "to_contract": self.to_contract,
            "aligned": self.aligned,
            "blocked": self.blocked,
            "items_total": len(self.mappings),
            "items_to_migrate": self.changed_count,
            "mappings": [mapping.as_dict() for mapping in self.mappings],
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
        }


@dataclass
class MigrationReport:
    plans: list[SourceMigrationPlan]

    @property
    def conflicts(self) -> list[MigrationConflict]:
        return [conflict for plan in self.plans for conflict in plan.conflicts]

    @property
    def blocked(self) -> bool:
        return any(plan.blocked for plan in self.plans)

    @property
    def aligned_sources(self) -> int:
        return sum(1 for plan in self.plans if plan.aligned)

    @property
    def items_to_migrate(self) -> int:
        return sum(plan.changed_count for plan in self.plans)

    def plan_for(self, source_id: str) -> Optional[SourceMigrationPlan]:
        for plan in self.plans:
            if plan.source_id == source_id:
                return plan
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "sources_total": len(self.plans),
            "sources_aligned": self.aligned_sources,
            "sources_with_conflicts": sum(1 for plan in self.plans if plan.blocked),
            "items_to_migrate": self.items_to_migrate,
            "plans": [plan.as_dict() for plan in self.plans],
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
        }


class MigrationBlocked(Exception):
    """Raised when an apply is attempted with unresolved conflicts."""

    def __init__(self, report: MigrationReport):
        self.report = report
        codes = sorted({conflict.code for conflict in report.conflicts})
        super().__init__(
            f"身份迁移被阻止：{len(report.conflicts)} 个冲突（{', '.join(codes)}），"
            "未改动任何条目"
        )


def _reconstructed_item(row: Mapping[str, Any]) -> ParsedItem:
    """Rebuild the identity-relevant ParsedItem from a stored ledger row."""

    return ParsedItem(
        raw_id=row["raw_id"], title=row["title"], link=row["link"],
        summary=None, content=None, published_at=row["published_at"],
    )


def plan_source_migration(
    source_id: str,
    rows: Sequence[Mapping[str, Any]],
    from_contract: str,
    to_contract: str,
) -> SourceMigrationPlan:
    """Pure planning: recompute keys under ``to_contract`` and detect clashes.

    The function never writes.  A plan is only conflict-free when the
    old->new relationship is an unambiguous one-to-one mapping.
    """

    validate_contract(from_contract)
    validate_contract(to_contract)
    plan = SourceMigrationPlan(source_id, from_contract, to_contract)
    if from_contract == to_contract:
        # Already aligned: the stored keys must remain exactly as they are.
        plan.mappings = [IdentityMapping(row["item_key"], row["item_key"], "unchanged")
                         for row in rows]
        return plan

    by_new_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        try:
            identity = identify_item(_reconstructed_item(row), to_contract)
        except IdentityError:
            # A row that loses all identity under the new contract cannot be
            # moved automatically.  Treat it as a blocking split-like conflict
            # rather than dropping the entry.
            plan.conflicts.append(MigrationConflict(
                source_id, CONFLICT_SPLIT,
                (row["item_key"],), (),
                "条目在目标契约下无法重新计算身份（id / link / title+date 均缺失）",
            ))
            continue
        mapping = IdentityMapping(row["item_key"], identity.item_key, identity.kind)
        plan.mappings.append(mapping)
        by_new_key.setdefault(identity.item_key, []).append(row)

    for new_key, members in by_new_key.items():
        if len(members) < 2:
            continue
        old_keys = tuple(sorted(member["item_key"] for member in members))
        hashes = {member["content_hash"] for member in members}
        if len(hashes) > 1:
            code = CONFLICT_MERGE_CONTENT
            reason = "多个旧身份映射到同一新身份且内容指纹不同，自动合并会丢弃内容"
        else:
            code = CONFLICT_MERGE_DUPLICATE
            reason = "多个旧身份映射到同一新身份，无法在无人工裁决下安全合并"
        plan.conflicts.append(MigrationConflict(
            source_id, code, old_keys, (new_key,), reason,
        ))

    # Defensive: more than one distinct new key for one stored key can only
    # arise from ledger drift (duplicated/corrupt rows), never from a pure
    # recomputation — surface it instead of renaming arbitrarily.
    seen_pairs: dict[str, set[str]] = {}
    for mapping in plan.mappings:
        seen_pairs.setdefault(mapping.old_key, set()).add(mapping.new_key)
    for old_key, new_keys in seen_pairs.items():
        if len(new_keys) > 1:
            plan.conflicts.append(MigrationConflict(
                source_id, CONFLICT_SPLIT, (old_key,), tuple(sorted(new_keys)),
                "同一旧身份对应多个新身份，禁止自动拆分",
            ))
    return plan


def plan_migration(store: Store, targets: Mapping[str, str]) -> MigrationReport:
    """Build a read-only migration plan for ``{source_id: target_contract}``."""

    plans: list[SourceMigrationPlan] = []
    for source_id in sorted(targets):
        to_contract = validate_contract(targets[source_id])
        if not store.has_source(source_id):
            plan = SourceMigrationPlan(source_id, IDENTITY_DEFAULT, to_contract)
            plan.conflicts.append(MigrationConflict(
                source_id, CONFLICT_UNKNOWN_SOURCE, (), (),
                "归档中不存在该来源（从未成功抓取或源 id 写错），无可迁移条目",
            ))
            plans.append(plan)
            continue
        from_contract = store.get_identity_contract(source_id) or IDENTITY_DEFAULT
        rows = store.get_identity_rows(source_id)
        plan = plan_source_migration(source_id, rows, from_contract, to_contract)
        ledger = store.get_migration_records(source_id)
        if ledger:
            latest = ledger[-1]["to_contract"]
            configured = store.get_configured_contract(source_id)
            # The sources row and the ledger can only disagree when a write
            # was interrupted or the database was edited out of band.
            if configured is not None and configured != latest:
                plan.conflicts.append(MigrationConflict(
                    source_id, CONFLICT_LEDGER_DRIFT, (), (),
                    f"迁移台账最新契约为 {latest!r}，但 sources 表记录为 {configured!r}",
                ))
        plans.append(plan)
    return MigrationReport(plans)


def apply_migration(
    store: Store,
    targets: Mapping[str, str],
    observed_at: str,
) -> MigrationReport:
    """Apply a conflict-free migration; raise without writing otherwise.

    Re-running after an interruption is idempotent: sources whose ledger
    already matches the target contract report as aligned and contribute
    zero renames, and every per-source transaction is atomic.
    """

    report = plan_migration(store, targets)
    if report.blocked:
        raise MigrationBlocked(report)
    for plan in report.plans:
        if plan.aligned:
            continue
        # Apply even when no key changes: the ledger row records that the
        # source's contract was explicitly brought to the target.
        store.apply_identity_migration(
            plan.source_id, plan.mappings,
            plan.from_contract, plan.to_contract, observed_at,
        )
    return report
