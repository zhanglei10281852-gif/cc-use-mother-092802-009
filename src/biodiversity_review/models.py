"""领域模型：项目、边界版本、成果申报、冲突记录与基金汇总快照。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .errors import ValidationError


class ClaimStatus(str, Enum):
    """成果申报状态机中的状态。"""

    DRAFT = "draft"  # 草稿
    SUBMITTED = "submitted"  # 已申报
    SUPPLEMENT_REQUESTED = "supplement_requested"  # 秘书处要求补充材料
    UNDER_REVIEW = "under_review"  # 专家复核中
    APPROVED = "approved"  # 已批准
    REJECTED = "rejected"  # 已驳回
    BLOCKED = "blocked"  # 因重复申报被拦截
    WITHDRAWN = "withdrawn"  # 申报方已撤回
    REVOKED = "revoked"  # 批准后被撤销


#: 参与重复检测的“活跃”状态：这些申报正被当作有效成果对待。
ACTIVE_CLAIM_STATUSES = frozenset(
    {
        ClaimStatus.SUBMITTED,
        ClaimStatus.SUPPLEMENT_REQUESTED,
        ClaimStatus.UNDER_REVIEW,
        ClaimStatus.APPROVED,
    }
)


class ConflictDimension(str, Enum):
    """重复申报的冲突维度。"""

    PROTECTED_AREA = "protected_area"  # 同一片保护地
    BENEFICIARY_COMMUNITY = "beneficiary_community"  # 同一组受益社区
    ECOLOGICAL_SURVEY = "ecological_survey"  # 同一份生态调查


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class SnapshotStatus(str, Enum):
    """基金批次汇总快照的状态。"""

    PUBLISHED = "published"  # 当前权威版本
    STALE = "stale"  # 受成果撤销影响、等待重新汇总（数据保留）
    SUPERSEDED = "superseded"  # 已被更新版本取代（数据保留可查）


@dataclass(frozen=True)
class IndicatorDefinition:
    """项目自定义的成果指标；不同国家项目的指标定义可以不同。"""

    code: str
    name: str
    unit: str
    target: float | None = None


@dataclass(frozen=True)
class ProjectCycle:
    """项目周期。"""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValidationError("项目周期结束日期早于开始日期")

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass
class Project:
    """受资助项目：记录资助国、申报方、周期、指标定义与所属基金批次。"""

    project_id: str
    name: str
    country_code: str
    applicant_org: str
    cycle: ProjectCycle
    indicators: dict[str, IndicatorDefinition]
    fund_batch_id: str


@dataclass(frozen=True)
class BoundaryVersion:
    """项目边界的一个核准版本；历史版本永不修改、永不删除。"""

    project_id: str
    revision: int
    site_codes: tuple[str, ...]
    effective_from: date
    rationale: str  # 核准理由（首版为立项理由）
    approved_by: str
    recorded_at: datetime
    supersedes: int | None = None


@dataclass(frozen=True)
class EvidenceAttachment:
    """证据附件摘要：只保存可核验的元数据，不保存文件本体。"""

    attachment_id: str
    kind: str  # 如 survey_report / patrol_log / photo / shapefile
    title: str
    digest: str  # 内容摘要，如 sha256:...
    submitted_by: str
    submitted_at: datetime
    survey_ids: tuple[str, ...] = ()  # 附件关联的生态调查编号


@dataclass
class Claim:
    """一次成果申报。"""

    claim_id: str
    project_id: str
    period_start: date
    period_end: date
    measurements: dict[str, float]  # 指标编码 -> 达成值
    site_codes: tuple[str, ...]  # 申报成果所在的保护地
    community_ids: tuple[str, ...]  # 受益社区
    survey_ids: tuple[str, ...]  # 依据的生态调查
    evidence: list[EvidenceAttachment]
    status: ClaimStatus
    submitted_by: str
    reviewer: str | None = None
    decision_reason: str | None = None


@dataclass(frozen=True)
class ClaimScope:
    """申报用于重复检测的有效范围（含证据附件带出的调查编号）。"""

    site_codes: frozenset[str]
    community_ids: frozenset[str]
    survey_ids: frozenset[str]


def claim_scope(claim: Claim) -> ClaimScope:
    surveys = set(claim.survey_ids)
    for attachment in claim.evidence:
        surveys.update(attachment.survey_ids)
    return ClaimScope(
        site_codes=frozenset(claim.site_codes),
        community_ids=frozenset(claim.community_ids),
        survey_ids=frozenset(surveys),
    )


@dataclass(frozen=True)
class ConflictRecord:
    """一条重复申报冲突记录，附着在被拦截的申报上。"""

    conflict_id: str
    claim_id: str  # 被拦截的申报
    other_claim_id: str  # 已在流程中的对方申报
    other_project_id: str
    other_applicant_org: str
    dimension: ConflictDimension
    overlapping_keys: tuple[str, ...]  # 重叠的保护地/社区/调查编号
    status: ConflictStatus
    detected_at: datetime
    resolved_at: datetime | None = None
    resolution: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    """审计事件：每次状态迁移或关键操作追加一条，永不修改。"""

    seq: int
    entity_type: str  # claim / project / batch
    entity_id: str
    action: str
    actor: str
    at: datetime
    from_status: ClaimStatus | None = None
    to_status: ClaimStatus | None = None
    reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AggregateSnapshot:
    """基金批次汇总的一个已发布版本。

    快照不可变：成果撤销只在快照上追加受影响标记，绝不抹除已发布数据。
    """

    batch_id: str
    version: int
    status: SnapshotStatus
    totals: dict[str, float]  # 指标编码 -> 汇总值
    claim_ids: tuple[str, ...]  # 纳入本版的已批准申报
    revoked_claim_ids: tuple[str, ...]  # 发布时已知的被撤销申报（未计入）
    affected_by: tuple[str, ...]  # 发布后影响本版的被撤销申报
    supersedes: int | None
    superseded_by: int | None
    published_at: datetime
    published_by: str
