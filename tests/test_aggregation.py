"""基金批次汇总：发布后成果被撤销时，定位受影响版本并演进新版本，不抹除数据。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from biodiversity_review import ClaimStatus, SnapshotStatus  # noqa: E402
from helpers import approve, make_claim, make_service, register_project  # noqa: E402

BATCH = "FUND-2026-Q3"


class FundAggregationTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        register_project(self.service, "P-KE-1", applicant="OrgAlpha", sites=("SITE-1",))
        register_project(self.service, "P-KE-2", applicant="OrgBeta", sites=("SITE-9",))
        make_claim(
            self.service,
            "CLM-A1",
            project_id="P-KE-1",
            sites=("SITE-1",),
            communities=("COM-A",),
            surveys=("SVY-A",),
            measurements={"habitat_ha": 100.0, "patrols": 12},
        )
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-KE-2",
            sites=("SITE-9",),
            communities=("COM-B",),
            surveys=("SVY-B",),
            measurements={"habitat_ha": 50.0, "patrols": 3},
        )
        approve(self.service, "CLM-A1")
        approve(self.service, "CLM-B1")

    def test_version_evolution_after_revocation(self):
        # v1：两份批准成果都计入汇总
        v1 = self.service.publish_batch(BATCH, actor="秘书处-赵")
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.status, SnapshotStatus.PUBLISHED)
        self.assertEqual(v1.totals, {"habitat_ha": 150.0, "patrols": 15.0})
        self.assertEqual(v1.claim_ids, ("CLM-A1", "CLM-B1"))
        self.assertIsNone(v1.supersedes)

        # 撤销 CLM-B1：v1 被标记为受影响，数据原样保留
        _, affected = self.service.revoke_claim(
            "CLM-B1", actor="秘书处-赵", reason="证据链核验失败"
        )
        self.assertEqual([(s.batch_id, s.version) for s in affected], [(BATCH, 1)])

        v1_after = self.service.batch_snapshots(BATCH)[0]
        self.assertEqual(v1_after.status, SnapshotStatus.STALE)
        self.assertEqual(v1_after.affected_by, ("CLM-B1",))
        self.assertEqual(v1_after.totals, {"habitat_ha": 150.0, "patrols": 15.0})
        self.assertIn("CLM-B1", v1_after.claim_ids)

        # 能定位哪些已发布统计包含被撤销的成果
        located = self.service.affected_snapshots("CLM-B1")
        self.assertEqual([(s.batch_id, s.version) for s in located], [(BATCH, 1)])
        self.assertEqual(self.service.affected_snapshots("CLM-A1"), [(v1_after)])

        # 重新汇总：v2 剔除被撤销成果，v1 转为 SUPERSEDED 但完整可查
        v2 = self.service.publish_batch(BATCH, actor="秘书处-赵")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.status, SnapshotStatus.PUBLISHED)
        self.assertEqual(v2.supersedes, 1)
        self.assertEqual(v2.totals, {"habitat_ha": 100.0, "patrols": 12.0})
        self.assertEqual(v2.claim_ids, ("CLM-A1",))
        self.assertEqual(v2.revoked_claim_ids, ("CLM-B1",))

        snapshots = self.service.batch_snapshots(BATCH)
        self.assertEqual([s.version for s in snapshots], [1, 2])
        self.assertEqual(snapshots[0].status, SnapshotStatus.SUPERSEDED)
        self.assertEqual(snapshots[0].superseded_by, 2)
        # 旧版本数据不被抹除：总量与申报清单保持发布时的样子
        self.assertEqual(snapshots[0].totals, {"habitat_ha": 150.0, "patrols": 15.0})
        self.assertEqual(snapshots[0].claim_ids, ("CLM-A1", "CLM-B1"))

        # 撤销事件本身也记录了受影响的版本
        trail = self.service.claim_audit_trail("CLM-B1")
        revoked_event = [e for e in trail if e.action == "claim.revoked"][0]
        self.assertEqual(
            revoked_event.details["affected_snapshots"],
            [{"batch_id": BATCH, "version": 1}],
        )

    def test_revocation_before_first_publish(self):
        # 从未发布过的撤销不影响任何快照，但首次发布会剔除它
        _, affected = self.service.revoke_claim("CLM-B1", actor="秘书处-赵", reason="重复计算")
        self.assertEqual(affected, [])
        self.assertEqual(self.service.affected_snapshots("CLM-B1"), [])

        v1 = self.service.publish_batch(BATCH, actor="秘书处-赵")
        self.assertEqual(v1.claim_ids, ("CLM-A1",))
        self.assertEqual(v1.revoked_claim_ids, ("CLM-B1",))
        self.assertEqual(v1.totals, {"habitat_ha": 100.0, "patrols": 12.0})

    def test_revocation_marks_only_snapshots_containing_the_claim(self):
        # 另一批次的项目与成果
        register_project(
            self.service,
            "P-NP-1",
            applicant="OrgGamma",
            country="NP",
            batch="FUND-2026-Q4",
            sites=("SITE-7",),
        )
        make_claim(
            self.service,
            "CLM-C1",
            project_id="P-NP-1",
            sites=("SITE-7",),
            communities=("COM-C",),
            surveys=("SVY-C",),
            measurements={"habitat_ha": 10.0},
        )
        approve(self.service, "CLM-C1")
        self.service.publish_batch(BATCH, actor="秘书处-赵")
        self.service.publish_batch("FUND-2026-Q4", actor="秘书处-赵")

        self.service.revoke_claim("CLM-C1", actor="秘书处-赵", reason="现场核查不通过")
        q3 = self.service.batch_snapshots(BATCH)[0]
        q4 = self.service.batch_snapshots("FUND-2026-Q4")[0]
        # 只有包含被撤销成果的批次受影响，另一批次保持权威
        self.assertEqual(q3.status, SnapshotStatus.PUBLISHED)
        self.assertEqual(q4.status, SnapshotStatus.STALE)
        self.assertEqual(q4.affected_by, ("CLM-C1",))

    def test_only_approved_claims_are_aggregated(self):
        # 新增一份只申报未批准的成果，不计入汇总
        make_claim(
            self.service,
            "CLM-A2",
            project_id="P-KE-1",
            sites=("SITE-1",),
            communities=("COM-A",),
            surveys=("SVY-A2",),
            measurements={"habitat_ha": 999.0},
        )
        self.service.submit_claim("CLM-A2", actor="申报员-李")
        v1 = self.service.publish_batch(BATCH, actor="秘书处-赵")
        self.assertEqual(v1.totals, {"habitat_ha": 150.0, "patrols": 15.0})
        self.assertNotIn("CLM-A2", v1.claim_ids)
        self.assertEqual(
            self.service.get_claim("CLM-A2").status, ClaimStatus.SUBMITTED
        )


if __name__ == "__main__":
    unittest.main()
