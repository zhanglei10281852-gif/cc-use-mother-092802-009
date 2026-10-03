"""内存仓储：集中保存所有聚合，查询方法供服务层与测试使用。"""

from __future__ import annotations

from .errors import NotFoundError
from .models import (
    AggregateSnapshot,
    AuditEvent,
    BoundaryVersion,
    Claim,
    ConflictDimension,
    ConflictRecord,
    ConflictStatus,
    Project,
)


class InMemoryRepository:
    """按聚合分区的内存存储；生产实现可替换为数据库仓储。"""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.claims: dict[str, Claim] = {}
        self.boundaries: dict[str, list[BoundaryVersion]] = {}
        self.conflicts: dict[str, ConflictRecord] = {}
        self.snapshots: dict[str, list[AggregateSnapshot]] = {}
        self.audit_log: list[AuditEvent] = []
        self._counters: dict[str, int] = {}

    # ---- 编号与序号 ----

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def next_seq(self) -> int:
        self._counters["__seq__"] = self._counters.get("__seq__", 0) + 1
        return self._counters["__seq__"]

    # ---- 项目 ----

    def add_project(self, project: Project) -> None:
        self.projects[project.project_id] = project

    def get_project(self, project_id: str) -> Project:
        try:
            return self.projects[project_id]
        except KeyError:
            raise NotFoundError(f"项目不存在: {project_id}") from None

    # ---- 边界版本 ----

    def add_boundary(self, version: BoundaryVersion) -> None:
        self.boundaries.setdefault(version.project_id, []).append(version)

    def boundary_history(self, project_id: str) -> list[BoundaryVersion]:
        return sorted(self.boundaries.get(project_id, []), key=lambda b: b.revision)

    def current_boundary(self, project_id: str) -> BoundaryVersion:
        history = self.boundary_history(project_id)
        if not history:
            raise NotFoundError(f"项目没有边界版本: {project_id}")
        return history[-1]

    # ---- 申报 ----

    def add_claim(self, claim: Claim) -> None:
        self.claims[claim.claim_id] = claim

    def get_claim(self, claim_id: str) -> Claim:
        try:
            return self.claims[claim_id]
        except KeyError:
            raise NotFoundError(f"申报不存在: {claim_id}") from None

    def all_claims(self) -> list[Claim]:
        return list(self.claims.values())

    def claims_for_project(self, project_id: str) -> list[Claim]:
        return sorted(
            (c for c in self.claims.values() if c.project_id == project_id),
            key=lambda c: c.claim_id,
        )

    # ---- 冲突记录 ----

    def add_conflict(self, record: ConflictRecord) -> None:
        self.conflicts[record.conflict_id] = record

    def replace_conflict(self, record: ConflictRecord) -> None:
        if record.conflict_id not in self.conflicts:
            raise NotFoundError(f"冲突记录不存在: {record.conflict_id}")
        self.conflicts[record.conflict_id] = record

    def find_open_conflict(
        self, claim_id: str, other_claim_id: str, dimension: ConflictDimension
    ) -> ConflictRecord | None:
        for record in self.conflicts.values():
            if (
                record.claim_id == claim_id
                and record.other_claim_id == other_claim_id
                and record.dimension == dimension
                and record.status is ConflictStatus.OPEN
            ):
                return record
        return None

    def conflicts_involving_claim(self, claim_id: str) -> list[ConflictRecord]:
        return sorted(
            (
                r
                for r in self.conflicts.values()
                if r.claim_id == claim_id or r.other_claim_id == claim_id
            ),
            key=lambda r: r.conflict_id,
        )

    def conflicts_involving_project(self, project_id: str) -> list[ConflictRecord]:
        own = {c.claim_id for c in self.claims.values() if c.project_id == project_id}
        return sorted(
            (
                r
                for r in self.conflicts.values()
                if r.claim_id in own or r.other_project_id == project_id
            ),
            key=lambda r: r.conflict_id,
        )

    # ---- 汇总快照 ----

    def add_snapshot(self, snapshot: AggregateSnapshot) -> None:
        self.snapshots.setdefault(snapshot.batch_id, []).append(snapshot)

    def replace_snapshot(self, snapshot: AggregateSnapshot) -> None:
        versions = self.snapshots.get(snapshot.batch_id, [])
        for index, old in enumerate(versions):
            if old.version == snapshot.version:
                versions[index] = snapshot
                return
        raise NotFoundError(
            f"快照不存在: {snapshot.batch_id} v{snapshot.version}"
        )

    def snapshots_for_batch(self, batch_id: str) -> list[AggregateSnapshot]:
        return sorted(self.snapshots.get(batch_id, []), key=lambda s: s.version)

    def all_snapshots(self) -> list[AggregateSnapshot]:
        return [s for versions in self.snapshots.values() for s in versions]

    # ---- 审计日志 ----

    def add_event(self, event: AuditEvent) -> None:
        self.audit_log.append(event)

    def events_for(self, entity_type: str, entity_id: str) -> list[AuditEvent]:
        return [
            e
            for e in self.audit_log
            if e.entity_type == entity_type and e.entity_id == entity_id
        ]
