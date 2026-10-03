"""申报状态流：申报→补充材料→专家复核→批准/撤回，全程可审计。"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from biodiversity_review import (  # noqa: E402
    ClaimStatus,
    StateTransitionError,
    ValidationError,
)
from helpers import evidence, make_claim, make_service, register_project  # noqa: E402


class ClaimLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        register_project(self.service, "P-KE-1", sites=("SITE-1", "SITE-2"))

    def test_full_review_flow_is_auditable(self):
        make_claim(self.service, "CLM-1")
        self.service.submit_claim("CLM-1", actor="申报员-李")
        self.service.request_supplement("CLM-1", actor="秘书处-赵", reason="缺少巡护日志")
        self.service.submit_supplement(
            "CLM-1",
            [evidence(digest="sha256:ev-2", kind="patrol_log", title="巡护日志")],
            actor="申报员-李",
        )
        self.service.start_expert_review("CLM-1", reviewer="专家-王")
        self.service.approve_claim("CLM-1", actor="专家-王", reason="证据链完整")

        claim = self.service.get_claim("CLM-1")
        self.assertEqual(claim.status, ClaimStatus.APPROVED)
        self.assertEqual(claim.reviewer, "专家-王")
        self.assertEqual(len(claim.evidence), 2)

        trail = self.service.claim_audit_trail("CLM-1")
        self.assertEqual(
            [e.action for e in trail],
            [
                "claim.created",
                "claim.submitted",
                "claim.supplement_requested",
                "claim.supplemented",
                "claim.review_started",
                "claim.approved",
            ],
        )
        self.assertEqual(
            [e.to_status for e in trail],
            [
                ClaimStatus.DRAFT,
                ClaimStatus.SUBMITTED,
                ClaimStatus.SUPPLEMENT_REQUESTED,
                ClaimStatus.SUBMITTED,
                ClaimStatus.UNDER_REVIEW,
                ClaimStatus.APPROVED,
            ],
        )
        # 每次迁移都记录操作者、时间与前后状态，序号单调递增
        for event in trail[1:]:
            self.assertIsNotNone(event.from_status)
            self.assertTrue(event.actor)
            self.assertIsNotNone(event.at)
        self.assertEqual([e.seq for e in trail], sorted(e.seq for e in trail))
        # 补充材料的原因留在审计里
        self.assertEqual(trail[2].reason, "缺少巡护日志")

    def test_withdraw_during_review_is_terminal(self):
        make_claim(self.service, "CLM-1")
        self.service.submit_claim("CLM-1", actor="申报员-李")
        self.service.start_expert_review("CLM-1", reviewer="专家-王")
        self.service.withdraw_claim("CLM-1", actor="申报员-李", reason="数据需重新整理")

        claim = self.service.get_claim("CLM-1")
        self.assertEqual(claim.status, ClaimStatus.WITHDRAWN)
        with self.assertRaises(StateTransitionError):
            self.service.approve_claim("CLM-1", actor="专家-王")
        trail = self.service.claim_audit_trail("CLM-1")
        self.assertEqual(trail[-1].action, "claim.withdrawn")
        self.assertEqual(trail[-1].reason, "数据需重新整理")

    def test_invalid_transitions_are_rejected(self):
        make_claim(self.service, "CLM-1")
        # 草稿不能直接进专家复核
        with self.assertRaises(StateTransitionError):
            self.service.start_expert_review("CLM-1", reviewer="专家-王")
        self.service.submit_claim("CLM-1", actor="申报员-李")
        # 未经复核不能批准
        with self.assertRaises(StateTransitionError):
            self.service.approve_claim("CLM-1", actor="专家-王")
        # 已申报状态不能重复申报
        with self.assertRaises(StateTransitionError):
            self.service.submit_claim("CLM-1", actor="申报员-李")

    def test_submit_requires_evidence(self):
        self.service.create_claim(
            claim_id="CLM-NOEV",
            project_id="P-KE-1",
            period_start=date(2026, 2, 1),
            period_end=date(2026, 6, 30),
            measurements={"habitat_ha": 10.0},
            site_codes=("SITE-1",),
            evidence=[],
            submitted_by="申报员-李",
        )
        with self.assertRaises(ValidationError):
            self.service.submit_claim("CLM-NOEV", actor="申报员-李")

    def test_claim_shape_validation(self):
        # 未定义的指标
        with self.assertRaises(ValidationError):
            make_claim(self.service, "CLM-BAD-1", measurements={"unknown": 1.0})
        # 成果周期超出项目周期
        with self.assertRaises(ValidationError):
            self.service.create_claim(
                claim_id="CLM-BAD-2",
                project_id="P-KE-1",
                period_start=date(2026, 2, 1),
                period_end=date(2028, 6, 30),
                measurements={"habitat_ha": 1.0},
                site_codes=("SITE-1",),
                evidence=[evidence()],
                submitted_by="申报员-李",
            )
        # 保护地超出当前边界
        with self.assertRaises(ValidationError):
            make_claim(self.service, "CLM-BAD-3", sites=("SITE-404",))
        # 指标数值为负
        with self.assertRaises(ValidationError):
            make_claim(self.service, "CLM-BAD-4", measurements={"habitat_ha": -1.0})

    def test_revoke_requires_approved_status_and_reason(self):
        make_claim(self.service, "CLM-1")
        with self.assertRaises(StateTransitionError):
            self.service.revoke_claim("CLM-1", actor="秘书处-赵", reason="证据失效")

        self.service.submit_claim("CLM-1", actor="申报员-李")
        self.service.start_expert_review("CLM-1", reviewer="专家-王")
        self.service.approve_claim("CLM-1", actor="专家-王")
        with self.assertRaises(ValidationError):
            self.service.revoke_claim("CLM-1", actor="秘书处-赵", reason="  ")

        claim, _ = self.service.revoke_claim("CLM-1", actor="秘书处-赵", reason="证据链核验失败")
        self.assertEqual(claim.status, ClaimStatus.REVOKED)
        self.assertEqual(claim.decision_reason, "证据链核验失败")
        # 撤销是终态
        with self.assertRaises(StateTransitionError):
            self.service.withdraw_claim("CLM-1", actor="申报员-李")


if __name__ == "__main__":
    unittest.main()
