"""测试共享辅助：确定性时钟、项目注册与申报构造。"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from biodiversity_review import IndicatorDefinition, ReviewService  # noqa: E402


class FakeClock:
    """每次调用前进一分钟，保证审计事件时间戳有序且确定。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._t = start or datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        now = self._t
        self._t += timedelta(minutes=1)
        return now


DEFAULT_INDICATORS = [
    IndicatorDefinition(code="habitat_ha", name="受保护栖息地面积", unit="ha"),
    IndicatorDefinition(code="patrols", name="反盗猎巡护次数", unit="count"),
]


def make_service() -> ReviewService:
    return ReviewService(clock=FakeClock())


def register_project(
    service: ReviewService,
    project_id: str = "P-KE-1",
    applicant: str = "OrgAlpha",
    country: str = "KE",
    batch: str = "FUND-2026-Q3",
    sites=("SITE-1", "SITE-2"),
    indicators=None,
):
    return service.register_project(
        project_id=project_id,
        name=f"项目{project_id}",
        country_code=country,
        applicant_org=applicant,
        cycle_start=date(2026, 1, 1),
        cycle_end=date(2027, 12, 31),
        indicators=indicators if indicators is not None else DEFAULT_INDICATORS,
        fund_batch_id=batch,
        boundary_sites=sites,
        boundary_effective_from=date(2026, 1, 1),
        rationale="立项核准边界",
        approved_by="秘书处",
    )


def evidence(
    digest: str = "sha256:ev-1",
    survey_ids=(),
    kind: str = "survey_report",
    title: str = "生态调查报告",
) -> dict:
    return {"kind": kind, "title": title, "digest": digest, "survey_ids": tuple(survey_ids)}


def make_claim(
    service: ReviewService,
    claim_id: str = "CLM-1",
    project_id: str = "P-KE-1",
    sites=("SITE-1",),
    communities=("COM-1",),
    surveys=("SVY-1",),
    measurements=None,
    ev=None,
):
    return service.create_claim(
        claim_id=claim_id,
        project_id=project_id,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 6, 30),
        measurements=measurements if measurements is not None else {"habitat_ha": 100.0},
        site_codes=sites,
        community_ids=communities,
        survey_ids=surveys,
        evidence=[ev if ev is not None else evidence()],
        submitted_by="申报员-李",
    )


def approve(service: ReviewService, claim_id: str, reviewer: str = "专家-王"):
    """让一份无冲突的申报走完全部正向流程。"""
    service.submit_claim(claim_id, actor="申报员-李")
    service.start_expert_review(claim_id, reviewer=reviewer)
    service.approve_claim(claim_id, actor=reviewer, reason="证据链完整")
