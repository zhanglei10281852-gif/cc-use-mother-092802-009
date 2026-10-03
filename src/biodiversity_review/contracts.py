"""项目边界、指标定义与成果证据契约。

这些值对象对外暴露给 API 与持久化层，全部不可变，确保审查过程中
引用的边界版本、证据摘要不会被悄悄改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Mapping


class SubmissionStatus(str, Enum):
    """申报材料的可审计状态流。"""

    SUBMITTED = "submitted"                    # 已申报
    AWAITING_SUPPLEMENT = "awaiting_supplement"  # 专家要求补充
    UNDER_REVIEW = "under_review"              # 专家复核中
    APPROVED = "approved"                      # 已批准（计入基金成果）
    REJECTED = "rejected"                      # 已驳回（终态）
    WITHDRAWN = "withdrawn"                    # 已撤回（终态，历史保留）


class ClaimStatus(str, Enum):
    """单条成果声明的状态。"""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REVOKED = "revoked"      # 批准后被撤销（如调查造假）
    WITHDRAWN = "withdrawn"  # 随申报整体撤回


class ReviewDecision(str, Enum):
    """专家复核结论。"""

    REQUEST_SUPPLEMENT = "request_supplement"
    RECOMMEND_APPROVAL = "recommend_approval"
    REJECT = "reject"


class ConflictKind(str, Enum):
    """重复计量冲突的三个维度。"""

    SHARED_SITE = "shared_site"                  # 同一片保护地
    SHARED_BENEFICIARY = "shared_beneficiary"    # 同一组受益社区
    SHARED_SURVEY = "shared_survey"              # 同一份生态调查（证据摘要相同）


class ConflictStatus(str, Enum):
    OPEN = "open"                          # 待秘书处裁决，阻止批准
    RESOLVED = "resolved"                  # 已裁决为合理共享（如联合资助），附理由
    CONFIRMED_DUPLICATE = "confirmed_duplicate"  # 裁决为重复申报，阻止批准


@dataclass(frozen=True)
class IndicatorDef:
    """某国项目周期内一个成果指标的定义。"""

    code: str
    name: str
    unit: str = "count"

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "unit": self.unit}


@dataclass(frozen=True)
class CountryCycle:
    """一个国家的项目周期：允许的里程碑与指标定义。"""

    country_code: str
    cycle_code: str
    name: str
    indicators: Mapping[str, IndicatorDef] = field(default_factory=dict)
    milestones: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "country_code": self.country_code,
            "cycle_code": self.cycle_code,
            "name": self.name,
            "indicators": {k: v.to_dict() for k, v in self.indicators.items()},
            "milestones": list(self.milestones),
        }


@dataclass(frozen=True)
class ProjectBoundary:
    """项目保护地边界的一个版本。

    revision=0 是立项时的原始边界；每次修订追加新版本，
    旧版本连同核准人与核准理由一并保留。
    """

    project_id: str
    revision: int
    country_code: str
    site_codes: tuple[str, ...]
    effective_from: date
    approved_by: str | None = None
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "revision": self.revision,
            "country_code": self.country_code,
            "site_codes": list(self.site_codes),
            "effective_from": self.effective_from.isoformat(),
            "approved_by": self.approved_by,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class OutcomeClaim:
    """一条成果声明。

    site_codes 为空时在申报瞬间从项目当前边界快照继承；
    evidence_digest 是生态调查等证据文件的摘要（如 sha256:...），
    不同申报方复用同一摘要即视为复用同一份调查。
    """

    claim_id: str
    project_id: str
    outcome_code: str
    beneficiary_group: str
    evidence_digest: str
    site_codes: tuple[str, ...] = ()
    period: str | None = None
    measure: float = 1.0

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "project_id": self.project_id,
            "outcome_code": self.outcome_code,
            "beneficiary_group": self.beneficiary_group,
            "evidence_digest": self.evidence_digest,
            "site_codes": list(self.site_codes),
            "period": self.period,
            "measure": self.measure,
        }


@dataclass(frozen=True)
class AttachmentSummary:
    """证据附件摘要（只登记摘要与元数据，不承载文件本体）。"""

    attachment_id: str
    filename: str
    content_type: str
    sha256: str
    summary: str
    uploaded_by: str

    def to_dict(self) -> dict:
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "sha256": self.sha256,
            "summary": self.summary,
            "uploaded_by": self.uploaded_by,
        }
