"""申报状态机：定义允许的状态迁移，保证状态流可审计。"""

from .errors import StateTransitionError
from .models import ClaimStatus

#: 允许的状态迁移表。BLOCKED 不是终态：冲突解除后可以重新申报。
TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.DRAFT: frozenset(
        {ClaimStatus.SUBMITTED, ClaimStatus.BLOCKED, ClaimStatus.WITHDRAWN}
    ),
    ClaimStatus.SUBMITTED: frozenset(
        {ClaimStatus.SUPPLEMENT_REQUESTED, ClaimStatus.UNDER_REVIEW, ClaimStatus.WITHDRAWN}
    ),
    ClaimStatus.SUPPLEMENT_REQUESTED: frozenset(
        {ClaimStatus.SUBMITTED, ClaimStatus.BLOCKED, ClaimStatus.WITHDRAWN}
    ),
    ClaimStatus.UNDER_REVIEW: frozenset(
        {
            ClaimStatus.APPROVED,
            ClaimStatus.REJECTED,
            ClaimStatus.SUPPLEMENT_REQUESTED,
            ClaimStatus.WITHDRAWN,
        }
    ),
    ClaimStatus.BLOCKED: frozenset({ClaimStatus.SUBMITTED, ClaimStatus.WITHDRAWN}),
    ClaimStatus.APPROVED: frozenset({ClaimStatus.REVOKED}),
    ClaimStatus.REJECTED: frozenset(),
    ClaimStatus.WITHDRAWN: frozenset(),
    ClaimStatus.REVOKED: frozenset(),
}


def ensure_transition(current: ClaimStatus, target: ClaimStatus) -> None:
    """校验状态迁移是否合法，非法迁移抛出 StateTransitionError。"""
    if target not in TRANSITIONS[current]:
        raise StateTransitionError(
            f"不允许从状态 {current.value} 迁移到 {target.value}"
        )
