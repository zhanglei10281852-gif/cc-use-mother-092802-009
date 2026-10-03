"""基金批次汇总：发布不可变快照，撤销后定位受影响版本并演进新版本。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from .models import AggregateSnapshot, ClaimStatus, SnapshotStatus
from .repository import InMemoryRepository


class FundAggregationService:
    """按基金批次汇总已批准成果，并管理快照版本演进。"""

    def __init__(self, repo: InMemoryRepository, clock: Callable) -> None:
        self.repo = repo
        self.clock = clock

    def approved_claims(self, batch_id: str) -> list:
        project_ids = {
            p.project_id
            for p in self.repo.projects.values()
            if p.fund_batch_id == batch_id
        }
        return sorted(
            (
                c
                for c in self.repo.claims.values()
                if c.project_id in project_ids and c.status is ClaimStatus.APPROVED
            ),
            key=lambda c: c.claim_id,
        )

    def compute_totals(self, batch_id: str) -> dict[str, float]:
        totals: dict[str, float] = {}
        for claim in self.approved_claims(batch_id):
            for code, value in claim.measurements.items():
                totals[code] = totals.get(code, 0.0) + value
        return totals

    def publish(self, batch_id: str, actor: str) -> AggregateSnapshot:
        """发布新的汇总版本；此前的当前版本转为 SUPERSEDED（数据保留）。"""
        existing = self.repo.snapshots_for_batch(batch_id)
        version = (max(s.version for s in existing) + 1) if existing else 1
        currents = [
            s
            for s in existing
            if s.status in (SnapshotStatus.PUBLISHED, SnapshotStatus.STALE)
        ]
        supersedes = max((s.version for s in currents), default=None)
        revoked = sorted(
            c.claim_id
            for c in self.repo.claims.values()
            if c.status is ClaimStatus.REVOKED
            and self.repo.get_project(c.project_id).fund_batch_id == batch_id
        )
        approved = self.approved_claims(batch_id)
        snapshot = AggregateSnapshot(
            batch_id=batch_id,
            version=version,
            status=SnapshotStatus.PUBLISHED,
            totals=self.compute_totals(batch_id),
            claim_ids=tuple(c.claim_id for c in approved),
            revoked_claim_ids=tuple(revoked),
            affected_by=(),
            supersedes=supersedes,
            superseded_by=None,
            published_at=self.clock(),
            published_by=actor,
        )
        for old in currents:
            self.repo.replace_snapshot(
                replace(
                    old,
                    status=SnapshotStatus.SUPERSEDED,
                    superseded_by=version,
                )
            )
        self.repo.add_snapshot(snapshot)
        return snapshot

    def mark_affected(self, claim_id: str) -> list[AggregateSnapshot]:
        """成果被撤销后：把包含该成果的当前快照标记为 STALE。

        只追加受影响标记，快照中的已发布数据保持原样，绝不抹除。
        """
        affected: list[AggregateSnapshot] = []
        for snapshot in self.repo.all_snapshots():
            if claim_id not in snapshot.claim_ids:
                continue
            if snapshot.status not in (SnapshotStatus.PUBLISHED, SnapshotStatus.STALE):
                continue
            if claim_id in snapshot.affected_by:
                continue
            updated = replace(
                snapshot,
                status=SnapshotStatus.STALE,
                affected_by=snapshot.affected_by + (claim_id,),
            )
            self.repo.replace_snapshot(updated)
            affected.append(updated)
        return affected

    def affected_snapshots(self, claim_id: str) -> list[AggregateSnapshot]:
        """定位哪些已发布统计包含该成果（不论版本新旧）。"""
        return sorted(
            (s for s in self.repo.all_snapshots() if claim_id in s.claim_ids),
            key=lambda s: (s.batch_id, s.version),
        )

    def current_snapshot(self, batch_id: str) -> AggregateSnapshot | None:
        currents = [
            s
            for s in self.repo.snapshots_for_batch(batch_id)
            if s.status in (SnapshotStatus.PUBLISHED, SnapshotStatus.STALE)
        ]
        return currents[-1] if currents else None
