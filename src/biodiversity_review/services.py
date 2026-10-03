"""应用服务：申报状态流、边界版本、重复拦截、汇总发布与审查查询。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Iterable

from .aggregation import FundAggregationService
from .conflicts import DIMENSION_ATTR, ConflictDetector, ConflictFinding
from .errors import StateTransitionError, ValidationError
from .models import (
    AuditEvent,
    BoundaryVersion,
    Claim,
    ClaimStatus,
    ConflictRecord,
    ConflictStatus,
    EvidenceAttachment,
    IndicatorDefinition,
    Project,
    ProjectCycle,
    claim_scope,
)
from .repository import InMemoryRepository
from .statemachine import ensure_transition


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReviewService:
    """基金秘书处使用的成果审查后端。

    - 项目按国家/周期/指标注册，边界以不可变版本保存，变更必须附核准理由；
    - 成果申报沿 草稿→申报→(补充材料)→专家复核→批准/驳回 流转，全程审计；
    - 不同申报方共用同一保护地、受益社区或生态调查时，申报被拦截为 BLOCKED；
    - 批准的成果计入基金批次汇总；撤销后定位受影响的已发布版本，而非抹除数据。
    """

    def __init__(
        self,
        repo: InMemoryRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo or InMemoryRepository()
        self.clock = clock or _utcnow
        self.detector = ConflictDetector()
        self.aggregation = FundAggregationService(self.repo, self.clock)

    # ------------------------------------------------------------------
    # 项目注册与边界版本
    # ------------------------------------------------------------------

    def register_project(
        self,
        *,
        project_id: str,
        name: str,
        country_code: str,
        applicant_org: str,
        cycle_start,
        cycle_end,
        indicators: Iterable[IndicatorDefinition],
        fund_batch_id: str,
        boundary_sites: Iterable[str],
        boundary_effective_from,
        rationale: str,
        approved_by: str,
    ) -> Project:
        indicators = list(indicators)
        boundary_sites = tuple(boundary_sites)
        if project_id in self.repo.projects:
            raise ValidationError(f"项目编号已存在: {project_id}")
        if not rationale or not rationale.strip():
            raise ValidationError("首版边界必须记录核准理由")
        if not boundary_sites:
            raise ValidationError("项目边界至少包含一个保护地")
        indicator_map = {i.code: i for i in indicators}
        if len(indicator_map) != len(indicators):
            raise ValidationError("指标编码重复")
        project = Project(
            project_id=project_id,
            name=name,
            country_code=country_code,
            applicant_org=applicant_org,
            cycle=ProjectCycle(start=cycle_start, end=cycle_end),
            indicators=indicator_map,
            fund_batch_id=fund_batch_id,
        )
        self.repo.add_project(project)
        self.repo.add_boundary(
            BoundaryVersion(
                project_id=project_id,
                revision=1,
                site_codes=boundary_sites,
                effective_from=boundary_effective_from,
                rationale=rationale,
                approved_by=approved_by,
                recorded_at=self.clock(),
                supersedes=None,
            )
        )
        self._audit(
            "project",
            project_id,
            "project.registered",
            approved_by,
            details={
                "country_code": country_code,
                "fund_batch_id": fund_batch_id,
                "boundary_sites": list(boundary_sites),
            },
        )
        return project

    def change_boundary(
        self,
        project_id: str,
        *,
        site_codes: Iterable[str],
        effective_from,
        rationale: str,
        approved_by: str,
    ) -> BoundaryVersion:
        """变更项目边界：追加新版本，原边界与核准理由全部保留。"""
        self.repo.get_project(project_id)
        site_codes = tuple(site_codes)
        if not rationale or not rationale.strip():
            raise ValidationError("边界变更必须记录核准理由")
        if not site_codes:
            raise ValidationError("边界至少保留一个保护地")
        previous = self.repo.current_boundary(project_id)
        revision = BoundaryVersion(
            project_id=project_id,
            revision=previous.revision + 1,
            site_codes=site_codes,
            effective_from=effective_from,
            rationale=rationale,
            approved_by=approved_by,
            recorded_at=self.clock(),
            supersedes=previous.revision,
        )
        self.repo.add_boundary(revision)
        self._audit(
            "project",
            project_id,
            "boundary.changed",
            approved_by,
            reason=rationale,
            details={
                "from_revision": previous.revision,
                "to_revision": revision.revision,
                "removed_sites": sorted(set(previous.site_codes) - set(site_codes)),
                "added_sites": sorted(set(site_codes) - set(previous.site_codes)),
            },
        )
        return revision

    def boundary_history(self, project_id: str) -> list[BoundaryVersion]:
        self.repo.get_project(project_id)
        return self.repo.boundary_history(project_id)

    # ------------------------------------------------------------------
    # 成果申报与状态流
    # ------------------------------------------------------------------

    def create_claim(
        self,
        *,
        claim_id: str,
        project_id: str,
        period_start,
        period_end,
        measurements: dict[str, float],
        site_codes: Iterable[str],
        community_ids: Iterable[str] = (),
        survey_ids: Iterable[str] = (),
        evidence: Iterable[dict] = (),
        submitted_by: str,
    ) -> Claim:
        project = self.repo.get_project(project_id)
        if claim_id in self.repo.claims:
            raise ValidationError(f"申报编号已存在: {claim_id}")
        site_codes = tuple(site_codes)
        self._validate_claim_shape(
            project, period_start, period_end, measurements, site_codes
        )
        claim = Claim(
            claim_id=claim_id,
            project_id=project_id,
            period_start=period_start,
            period_end=period_end,
            measurements={k: float(v) for k, v in measurements.items()},
            site_codes=site_codes,
            community_ids=tuple(community_ids),
            survey_ids=tuple(survey_ids),
            evidence=self._build_attachments(evidence, submitted_by),
            status=ClaimStatus.DRAFT,
            submitted_by=submitted_by,
        )
        self.repo.add_claim(claim)
        self._audit(
            "claim",
            claim_id,
            "claim.created",
            submitted_by,
            to_status=ClaimStatus.DRAFT,
            details={"project_id": project_id},
        )
        return claim

    def submit_claim(self, claim_id: str, actor: str) -> Claim:
        """申报：先做重复检测，存在跨申报方冲突则拦截为 BLOCKED。"""
        claim = self.repo.get_claim(claim_id)
        if claim.status not in (ClaimStatus.DRAFT, ClaimStatus.BLOCKED):
            raise StateTransitionError(f"当前状态 {claim.status.value} 不能申报")
        project = self.repo.get_project(claim.project_id)
        self._validate_claim_shape(
            project,
            claim.period_start,
            claim.period_end,
            claim.measurements,
            claim.site_codes,
        )
        if not claim.evidence:
            raise ValidationError("申报必须至少附一份证据附件摘要")
        findings = self.detector.detect(
            claim, project, self._all_claims_with_projects()
        )
        if findings:
            records = [self._record_conflict(claim, f) for f in findings]
            conflict_ids = [r.conflict_id for r in records]
            if claim.status is ClaimStatus.DRAFT:
                self._transition(
                    claim,
                    ClaimStatus.BLOCKED,
                    "claim.blocked",
                    actor,
                    reason="与其他申报方存在重复成果",
                    details={"conflict_ids": conflict_ids},
                )
            else:
                self._audit(
                    "claim",
                    claim_id,
                    "claim.blocked",
                    actor,
                    from_status=ClaimStatus.BLOCKED,
                    to_status=ClaimStatus.BLOCKED,
                    reason="重复申报冲突仍未解除",
                    details={"conflict_ids": conflict_ids},
                )
            return claim
        self._transition(claim, ClaimStatus.SUBMITTED, "claim.submitted", actor)
        return claim

    def request_supplement(self, claim_id: str, actor: str, reason: str) -> Claim:
        """秘书处要求申报方补充材料。"""
        if not reason or not reason.strip():
            raise ValidationError("要求补充材料必须说明原因")
        claim = self.repo.get_claim(claim_id)
        self._transition(
            claim,
            ClaimStatus.SUPPLEMENT_REQUESTED,
            "claim.supplement_requested",
            actor,
            reason=reason,
        )
        return claim

    def submit_supplement(
        self, claim_id: str, attachments: Iterable[dict], actor: str
    ) -> Claim:
        """提交补充材料；补充的证据可能引入新的重复冲突，需重新检测。"""
        claim = self.repo.get_claim(claim_id)
        if claim.status is not ClaimStatus.SUPPLEMENT_REQUESTED:
            raise StateTransitionError(f"当前状态 {claim.status.value} 不能提交补充材料")
        attachments = list(attachments)
        if not attachments:
            raise ValidationError("补充材料不能为空")
        claim.evidence.extend(self._build_attachments(attachments, actor))
        project = self.repo.get_project(claim.project_id)
        findings = self.detector.detect(
            claim, project, self._all_claims_with_projects()
        )
        if findings:
            records = [self._record_conflict(claim, f) for f in findings]
            self._transition(
                claim,
                ClaimStatus.BLOCKED,
                "claim.blocked",
                actor,
                reason="补充材料引入新的重复成果冲突",
                details={"conflict_ids": [r.conflict_id for r in records]},
            )
        else:
            self._transition(
                claim,
                ClaimStatus.SUBMITTED,
                "claim.supplemented",
                actor,
                details={"added_attachments": len(attachments)},
            )
        return claim

    def start_expert_review(self, claim_id: str, reviewer: str) -> Claim:
        claim = self.repo.get_claim(claim_id)
        self._transition(
            claim, ClaimStatus.UNDER_REVIEW, "claim.review_started", reviewer
        )
        claim.reviewer = reviewer
        return claim

    def approve_claim(self, claim_id: str, actor: str, reason: str = "") -> Claim:
        claim = self.repo.get_claim(claim_id)
        self._transition(claim, ClaimStatus.APPROVED, "claim.approved", actor, reason=reason)
        claim.reviewer = claim.reviewer or actor
        claim.decision_reason = reason
        return claim

    def reject_claim(self, claim_id: str, actor: str, reason: str) -> Claim:
        if not reason or not reason.strip():
            raise ValidationError("驳回必须说明理由")
        claim = self.repo.get_claim(claim_id)
        self._transition(claim, ClaimStatus.REJECTED, "claim.rejected", actor, reason=reason)
        claim.decision_reason = reason
        self._resolve_conflicts_for(claim_id, "对方申报已被驳回")
        return claim

    def withdraw_claim(self, claim_id: str, actor: str, reason: str = "") -> Claim:
        claim = self.repo.get_claim(claim_id)
        self._transition(claim, ClaimStatus.WITHDRAWN, "claim.withdrawn", actor, reason=reason)
        self._resolve_conflicts_for(claim_id, "对方申报已撤回")
        return claim

    def revoke_claim(self, claim_id: str, actor: str, reason: str):
        """撤销已批准成果：定位受影响的已发布汇总，而不是抹除数据。"""
        if not reason or not reason.strip():
            raise ValidationError("撤销成果必须说明理由")
        claim = self.repo.get_claim(claim_id)
        ensure_transition(claim.status, ClaimStatus.REVOKED)
        old = claim.status
        claim.status = ClaimStatus.REVOKED
        claim.decision_reason = reason
        affected = self.aggregation.mark_affected(claim_id)
        self._audit(
            "claim",
            claim_id,
            "claim.revoked",
            actor,
            from_status=old,
            to_status=ClaimStatus.REVOKED,
            reason=reason,
            details={
                "affected_snapshots": [
                    {"batch_id": s.batch_id, "version": s.version} for s in affected
                ]
            },
        )
        self._resolve_conflicts_for(claim_id, "对方成果已撤销")
        return claim, affected

    def update_claim_scope(
        self,
        claim_id: str,
        actor: str,
        *,
        site_codes: Iterable[str] | None = None,
        community_ids: Iterable[str] | None = None,
        survey_ids: Iterable[str] | None = None,
    ) -> Claim:
        """调整草稿/被拦截申报的范围；重叠消除后相关冲突自动解除。"""
        claim = self.repo.get_claim(claim_id)
        if claim.status not in (ClaimStatus.DRAFT, ClaimStatus.BLOCKED):
            raise StateTransitionError("仅草稿或被拦截的申报可以调整范围")
        project = self.repo.get_project(claim.project_id)
        if site_codes is not None:
            site_codes = tuple(site_codes)
            self._validate_sites(project, site_codes)
            claim.site_codes = site_codes
        if community_ids is not None:
            claim.community_ids = tuple(community_ids)
        if survey_ids is not None:
            claim.survey_ids = tuple(survey_ids)
        self._audit(
            "claim",
            claim_id,
            "claim.scope_updated",
            actor,
            details={
                "site_codes": list(claim.site_codes),
                "community_ids": list(claim.community_ids),
                "survey_ids": list(claim.survey_ids),
            },
        )
        self._resolve_stale_conflicts(claim)
        return claim

    # ------------------------------------------------------------------
    # 基金批次汇总
    # ------------------------------------------------------------------

    def publish_batch(self, batch_id: str, actor: str):
        """发布批次汇总新版本；旧版本转为 SUPERSEDED，数据保留。"""
        snapshot = self.aggregation.publish(batch_id, actor)
        self._audit(
            "batch",
            batch_id,
            "batch.published",
            actor,
            details={
                "version": snapshot.version,
                "totals": snapshot.totals,
                "claim_ids": list(snapshot.claim_ids),
                "supersedes": snapshot.supersedes,
            },
        )
        return snapshot

    def affected_snapshots(self, claim_id: str):
        """定位哪些已发布统计包含该成果。"""
        self.repo.get_claim(claim_id)
        return self.aggregation.affected_snapshots(claim_id)

    def batch_snapshots(self, batch_id: str):
        return self.repo.snapshots_for_batch(batch_id)

    # ------------------------------------------------------------------
    # 审查人员查询 API
    # ------------------------------------------------------------------

    def project_evidence(self, project_id: str) -> dict:
        """按项目查看全部申报及其证据附件摘要。"""
        project = self.repo.get_project(project_id)
        boundary = self.repo.current_boundary(project_id)
        claims = []
        for claim in self.repo.claims_for_project(project_id):
            claims.append(
                {
                    "claim_id": claim.claim_id,
                    "status": claim.status,
                    "measurements": dict(claim.measurements),
                    "site_codes": list(claim.site_codes),
                    "community_ids": list(claim.community_ids),
                    "survey_ids": sorted(claim_scope(claim).survey_ids),
                    "evidence": [
                        {
                            "attachment_id": a.attachment_id,
                            "kind": a.kind,
                            "title": a.title,
                            "digest": a.digest,
                            "survey_ids": list(a.survey_ids),
                            "submitted_by": a.submitted_by,
                            "submitted_at": a.submitted_at,
                        }
                        for a in claim.evidence
                    ],
                }
            )
        return {
            "project_id": project.project_id,
            "name": project.name,
            "country_code": project.country_code,
            "applicant_org": project.applicant_org,
            "fund_batch_id": project.fund_batch_id,
            "current_boundary_revision": boundary.revision,
            "claims": claims,
        }

    def project_conflicts(self, project_id: str) -> dict:
        """按项目查看其申报与其他申报方之间的冲突关系。"""
        self.repo.get_project(project_id)
        return {
            "project_id": project_id,
            "conflicts": list(self.repo.conflicts_involving_project(project_id)),
        }

    def claim_audit_trail(self, claim_id: str) -> list[AuditEvent]:
        self.repo.get_claim(claim_id)
        return self.repo.events_for("claim", claim_id)

    def project_audit_trail(self, project_id: str) -> list[AuditEvent]:
        self.repo.get_project(project_id)
        return self.repo.events_for("project", project_id)

    def get_project(self, project_id: str) -> Project:
        return self.repo.get_project(project_id)

    def get_claim(self, claim_id: str) -> Claim:
        return self.repo.get_claim(claim_id)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _all_claims_with_projects(self) -> list[tuple[Claim, Project]]:
        return [
            (claim, self.repo.projects[claim.project_id])
            for claim in self.repo.all_claims()
        ]

    def _build_attachments(
        self, items: Iterable[dict], submitted_by: str
    ) -> list[EvidenceAttachment]:
        attachments = []
        for item in items:
            if not item.get("digest"):
                raise ValidationError("证据附件必须提供内容摘要 digest")
            attachments.append(
                EvidenceAttachment(
                    attachment_id=self.repo.next_id("ATT"),
                    kind=item.get("kind", "document"),
                    title=item.get("title", ""),
                    digest=item["digest"],
                    submitted_by=submitted_by,
                    submitted_at=self.clock(),
                    survey_ids=tuple(item.get("survey_ids", ())),
                )
            )
        return attachments

    def _validate_claim_shape(
        self, project: Project, period_start, period_end, measurements, site_codes
    ) -> None:
        if period_end < period_start:
            raise ValidationError("成果周期起止日期颠倒")
        cycle = project.cycle
        if not (cycle.contains(period_start) and cycle.contains(period_end)):
            raise ValidationError(
                f"成果周期 {period_start}~{period_end} 超出项目周期 "
                f"{cycle.start}~{cycle.end}"
            )
        if not measurements:
            raise ValidationError("至少填报一项指标")
        unknown = sorted(set(measurements) - set(project.indicators))
        if unknown:
            raise ValidationError(f"项目未定义的指标: {unknown}")
        for code, value in measurements.items():
            if value < 0:
                raise ValidationError(f"指标 {code} 的数值不能为负")
        self._validate_sites(project, site_codes)

    def _validate_sites(self, project: Project, site_codes) -> None:
        if not site_codes:
            raise ValidationError("申报必须关联至少一个保护地")
        boundary = self.repo.current_boundary(project.project_id)
        outside = sorted(set(site_codes) - set(boundary.site_codes))
        if outside:
            raise ValidationError(f"保护地不在项目当前边界内: {outside}")

    def _record_conflict(self, claim: Claim, finding: ConflictFinding) -> ConflictRecord:
        existing = self.repo.find_open_conflict(
            claim.claim_id, finding.other_claim_id, finding.dimension
        )
        if existing is not None:
            return existing
        record = ConflictRecord(
            conflict_id=self.repo.next_id("CNF"),
            claim_id=claim.claim_id,
            other_claim_id=finding.other_claim_id,
            other_project_id=finding.other_project_id,
            other_applicant_org=finding.other_applicant_org,
            dimension=finding.dimension,
            overlapping_keys=finding.overlapping_keys,
            status=ConflictStatus.OPEN,
            detected_at=self.clock(),
        )
        self.repo.add_conflict(record)
        return record

    def _resolve_conflicts_for(self, claim_id: str, resolution: str) -> None:
        """某申报退出活跃状态后，解除与之相关的全部未决冲突。"""
        now = self.clock()
        for record in self.repo.conflicts_involving_claim(claim_id):
            if record.status is ConflictStatus.OPEN:
                self.repo.replace_conflict(
                    replace(
                        record,
                        status=ConflictStatus.RESOLVED,
                        resolved_at=now,
                        resolution=resolution,
                    )
                )

    def _resolve_stale_conflicts(self, claim: Claim) -> None:
        """申报范围调整后，解除重叠已消失的未决冲突。"""
        scope = claim_scope(claim)
        now = self.clock()
        for record in self.repo.conflicts_involving_claim(claim.claim_id):
            if record.status is not ConflictStatus.OPEN:
                continue
            if record.claim_id != claim.claim_id:
                continue
            other = self.repo.claims.get(record.other_claim_id)
            if other is None:
                continue
            other_scope = claim_scope(other)
            attr = DIMENSION_ATTR[record.dimension]
            if not (getattr(scope, attr) & getattr(other_scope, attr)):
                self.repo.replace_conflict(
                    replace(
                        record,
                        status=ConflictStatus.RESOLVED,
                        resolved_at=now,
                        resolution="申报范围已调整，重叠消除",
                    )
                )

    def _transition(
        self,
        claim: Claim,
        target: ClaimStatus,
        action: str,
        actor: str,
        reason: str = "",
        details: dict | None = None,
    ) -> None:
        ensure_transition(claim.status, target)
        old = claim.status
        claim.status = target
        self._audit(
            "claim",
            claim.claim_id,
            action,
            actor,
            from_status=old,
            to_status=target,
            reason=reason,
            details=details,
        )

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        from_status: ClaimStatus | None = None,
        to_status: ClaimStatus | None = None,
        reason: str = "",
        details: dict | None = None,
    ) -> None:
        self.repo.add_event(
            AuditEvent(
                seq=self.repo.next_seq(),
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor=actor,
                at=self.clock(),
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                details=details or {},
            )
        )
