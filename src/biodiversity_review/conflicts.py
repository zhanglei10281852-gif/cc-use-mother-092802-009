"""重复申报检测：识别不同申报方对同一保护地、社区或生态调查的重复计算。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    ACTIVE_CLAIM_STATUSES,
    Claim,
    ConflictDimension,
    Project,
    claim_scope,
)

#: 冲突维度到申报范围属性的映射。
DIMENSION_ATTR = {
    ConflictDimension.PROTECTED_AREA: "site_codes",
    ConflictDimension.BENEFICIARY_COMMUNITY: "community_ids",
    ConflictDimension.ECOLOGICAL_SURVEY: "survey_ids",
}


@dataclass(frozen=True)
class ConflictFinding:
    """一次检测到的重叠（尚未落库）。"""

    dimension: ConflictDimension
    other_claim_id: str
    other_project_id: str
    other_applicant_org: str
    overlapping_keys: tuple[str, ...]


class ConflictDetector:
    """在新申报与既有活跃申报之间检测跨申报方的成果重复。"""

    def detect(
        self,
        claim: Claim,
        project: Project,
        others: list[tuple[Claim, Project]],
    ) -> list[ConflictFinding]:
        findings: list[ConflictFinding] = []
        scope = claim_scope(claim)
        for other, other_project in others:
            if other.claim_id == claim.claim_id:
                continue
            if other.project_id == claim.project_id:
                continue  # 同一项目内部的申报不互算重复
            if other_project.applicant_org == project.applicant_org:
                continue  # 同一申报方复用自己的成果不算重复
            if other.status not in ACTIVE_CLAIM_STATUSES:
                continue  # 已撤回/驳回/撤销/被拦截的申报不占成果份额
            other_scope = claim_scope(other)
            for dimension, attr in DIMENSION_ATTR.items():
                overlap = sorted(getattr(scope, attr) & getattr(other_scope, attr))
                if overlap:
                    findings.append(
                        ConflictFinding(
                            dimension=dimension,
                            other_claim_id=other.claim_id,
                            other_project_id=other.project_id,
                            other_applicant_org=other_project.applicant_org,
                            overlapping_keys=tuple(overlap),
                        )
                    )
        return findings
