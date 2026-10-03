"""测试共享夹具：构造一个有两个国家周期的审查服务。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from biodiversity_review.service import ReviewService  # noqa: E402
from biodiversity_review.storage import Store  # noqa: E402


def build_service() -> ReviewService:
    service = ReviewService(store=Store())
    # 尼泊尔周期：里程碑 m1，指标 habitat / forest
    service.register_cycle(
        country_code="np",
        cycle_code="NP-2025-2030",
        name="尼泊尔山地生态周期",
        indicators=[
            {"code": "habitat", "name": "栖息地恢复公顷", "unit": "hectare"},
            {"code": "forest", "name": "社区管护林公顷", "unit": "hectare"},
        ],
        milestones=["m1-inventory", "m2-midterm"],
    )
    # 巴西周期：指标 forest（按本国定义登记）
    service.register_cycle(
        country_code="br",
        cycle_code="BR-2026-2031",
        name="巴西大西洋林周期",
        indicators=[
            {"code": "forest", "name": "恢复林地面积", "unit": "hectare"},
            {"code": "survey", "name": "物种调查完成数", "unit": "count"},
        ],
        milestones=["baseline"],
    )
    return service


def make_project(service: ReviewService, project_id: str, country: str, sites):
    return service.register_project(
        project_id=project_id,
        country_code=country,
        site_codes=sites,
        effective_from="2026-01-01",
        actor="secretariat",
    )


def simple_claim(claim_id, *, outcome="habitat", group="comm-x", digest="sha256:d1",
                 sites=None, measure=10.0, period="2026H1"):
    claim = {
        "claim_id": claim_id,
        "outcome_code": outcome,
        "beneficiary_group": group,
        "evidence_digest": digest,
        "measure": measure,
        "period": period,
    }
    if sites is not None:
        claim["site_codes"] = sites
    return claim
