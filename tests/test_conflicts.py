"""重复申报拦截：同一保护地、受益社区或生态调查不得被不同申报方重复计算。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from biodiversity_review import (  # noqa: E402
    ClaimStatus,
    ConflictDimension,
    ConflictStatus,
    StateTransitionError,
)
from helpers import make_claim, make_service, register_project  # noqa: E402


class DuplicateClaimTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        # 两个不同申报方的项目，边界都覆盖 SITE-1（现实中的重叠保护地）
        register_project(self.service, "P-A", applicant="OrgAlpha", sites=("SITE-1", "SITE-2"))
        register_project(self.service, "P-B", applicant="OrgBeta", sites=("SITE-1", "SITE-9"))
        make_claim(
            self.service,
            "CLM-A1",
            project_id="P-A",
            sites=("SITE-1",),
            communities=("COM-1",),
            surveys=("SVY-1",),
        )
        self.service.submit_claim("CLM-A1", actor="申报员-甲")

    def test_duplicate_protected_area_is_blocked(self):
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-B",
            sites=("SITE-1",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        claim = self.service.submit_claim("CLM-B1", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.BLOCKED)

        view = self.service.project_conflicts("P-B")
        self.assertEqual(len(view["conflicts"]), 1)
        record = view["conflicts"][0]
        self.assertEqual(record.dimension, ConflictDimension.PROTECTED_AREA)
        self.assertEqual(record.overlapping_keys, ("SITE-1",))
        self.assertEqual(record.other_claim_id, "CLM-A1")
        self.assertEqual(record.other_project_id, "P-A")
        self.assertEqual(record.other_applicant_org, "OrgAlpha")
        self.assertEqual(record.status, ConflictStatus.OPEN)
        # 冲突双方项目都能看到同一条记录
        self.assertEqual(
            self.service.project_conflicts("P-A")["conflicts"][0].conflict_id,
            record.conflict_id,
        )
        # 被拦截的申报不能进入专家复核
        with self.assertRaises(StateTransitionError):
            self.service.start_expert_review("CLM-B1", reviewer="专家-王")

    def test_duplicate_survey_and_community_are_blocked(self):
        # 复用同一份生态调查（保护地不同）
        make_claim(
            self.service,
            "CLM-B2",
            project_id="P-B",
            sites=("SITE-9",),
            communities=("COM-9",),
            surveys=("SVY-1",),
        )
        claim = self.service.submit_claim("CLM-B2", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.BLOCKED)
        record = self.service.project_conflicts("P-B")["conflicts"][0]
        self.assertEqual(record.dimension, ConflictDimension.ECOLOGICAL_SURVEY)
        self.assertEqual(record.overlapping_keys, ("SVY-1",))

        # 覆盖同一组受益社区（保护地、调查均不同）
        make_claim(
            self.service,
            "CLM-B3",
            project_id="P-B",
            sites=("SITE-9",),
            communities=("COM-1",),
            surveys=("SVY-9",),
        )
        claim = self.service.submit_claim("CLM-B3", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.BLOCKED)
        dims = {
            r.dimension for r in self.service.project_conflicts("P-B")["conflicts"]
        }
        self.assertIn(ConflictDimension.BENEFICIARY_COMMUNITY, dims)

    def test_independent_claim_passes(self):
        make_claim(
            self.service,
            "CLM-B4",
            project_id="P-B",
            sites=("SITE-9",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        claim = self.service.submit_claim("CLM-B4", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.SUBMITTED)
        self.service.start_expert_review("CLM-B4", reviewer="专家-王")
        claim = self.service.approve_claim("CLM-B4", actor="专家-王")
        self.assertEqual(claim.status, ClaimStatus.APPROVED)
        self.assertEqual(self.service.project_conflicts("P-B")["conflicts"], [])

    def test_same_applicant_sharing_is_not_blocked(self):
        # 同一申报方的另一个项目复用自己的保护地与调查，不算重复
        register_project(
            self.service, "P-A2", applicant="OrgAlpha", country="TZ", sites=("SITE-1",)
        )
        make_claim(
            self.service,
            "CLM-A2",
            project_id="P-A2",
            sites=("SITE-1",),
            communities=("COM-1",),
            surveys=("SVY-1",),
        )
        claim = self.service.submit_claim("CLM-A2", actor="申报员-甲")
        self.assertEqual(claim.status, ClaimStatus.SUBMITTED)

    def test_resubmit_after_conflicting_claim_withdrawn(self):
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-B",
            sites=("SITE-1",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        self.service.submit_claim("CLM-B1", actor="申报员-乙")
        self.assertEqual(self.service.get_claim("CLM-B1").status, ClaimStatus.BLOCKED)

        self.service.withdraw_claim("CLM-A1", actor="申报员-甲", reason="合并到其他申报")
        record = self.service.project_conflicts("P-B")["conflicts"][0]
        self.assertEqual(record.status, ConflictStatus.RESOLVED)
        self.assertEqual(record.resolution, "对方申报已撤回")
        self.assertIsNotNone(record.resolved_at)

        claim = self.service.submit_claim("CLM-B1", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.SUBMITTED)

    def test_scope_update_clears_conflict(self):
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-B",
            sites=("SITE-1",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        self.service.submit_claim("CLM-B1", actor="申报员-乙")
        self.assertEqual(self.service.get_claim("CLM-B1").status, ClaimStatus.BLOCKED)

        # 申报方把成果范围调整到不重叠的保护地
        self.service.update_claim_scope("CLM-B1", actor="申报员-乙", site_codes=("SITE-9",))
        record = self.service.project_conflicts("P-B")["conflicts"][0]
        self.assertEqual(record.status, ConflictStatus.RESOLVED)
        self.assertEqual(record.resolution, "申报范围已调整，重叠消除")

        claim = self.service.submit_claim("CLM-B1", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.SUBMITTED)

    def test_resubmit_while_conflict_persists_stays_blocked(self):
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-B",
            sites=("SITE-1",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        self.service.submit_claim("CLM-B1", actor="申报员-乙")
        claim = self.service.submit_claim("CLM-B1", actor="申报员-乙")
        self.assertEqual(claim.status, ClaimStatus.BLOCKED)
        # 同一冲突不重复建档，但两次拦截都留有审计
        self.assertEqual(len(self.service.project_conflicts("P-B")["conflicts"]), 1)
        actions = [e.action for e in self.service.claim_audit_trail("CLM-B1")]
        self.assertEqual(actions.count("claim.blocked"), 2)


if __name__ == "__main__":
    unittest.main()
