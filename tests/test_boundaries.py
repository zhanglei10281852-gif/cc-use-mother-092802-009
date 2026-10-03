"""项目边界变更：原边界与核准理由必须保留，新申报按当前边界校验。"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from biodiversity_review import ValidationError  # noqa: E402
from helpers import make_claim, make_service, register_project  # noqa: E402


class BoundaryVersionTests(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        register_project(self.service, "P-KE-1", sites=("SITE-1", "SITE-2"))

    def test_original_boundary_and_rationale_are_retained(self):
        self.service.change_boundary(
            "P-KE-1",
            site_codes=("SITE-2", "SITE-3"),
            effective_from=date(2026, 7, 1),
            rationale="卫星复核后剔除已退化地块，纳入新生态廊道",
            approved_by="秘书处-张",
        )
        history = self.service.boundary_history("P-KE-1")
        self.assertEqual([b.revision for b in history], [1, 2])

        original, current = history
        # 原边界与立项理由原样保留
        self.assertEqual(original.site_codes, ("SITE-1", "SITE-2"))
        self.assertEqual(original.rationale, "立项核准边界")
        self.assertIsNone(original.supersedes)
        # 新版本记录核准理由并指向前版
        self.assertEqual(current.site_codes, ("SITE-2", "SITE-3"))
        self.assertEqual(current.supersedes, 1)
        self.assertEqual(current.rationale, "卫星复核后剔除已退化地块，纳入新生态廊道")
        self.assertEqual(current.approved_by, "秘书处-张")

        events = self.service.project_audit_trail("P-KE-1")
        self.assertEqual(
            [e.action for e in events], ["project.registered", "boundary.changed"]
        )
        self.assertEqual(events[-1].details["removed_sites"], ["SITE-1"])
        self.assertEqual(events[-1].details["added_sites"], ["SITE-3"])
        self.assertEqual(events[-1].reason, "卫星复核后剔除已退化地块，纳入新生态廊道")

    def test_boundary_change_requires_rationale(self):
        with self.assertRaises(ValidationError):
            self.service.change_boundary(
                "P-KE-1",
                site_codes=("SITE-2",),
                effective_from=date(2026, 7, 1),
                rationale="   ",
                approved_by="秘书处-张",
            )
        # 未成功的变更不产生新版本
        self.assertEqual(len(self.service.boundary_history("P-KE-1")), 1)

    def test_new_claims_validated_against_current_boundary(self):
        self.service.change_boundary(
            "P-KE-1",
            site_codes=("SITE-2", "SITE-3"),
            effective_from=date(2026, 7, 1),
            rationale="边界修正",
            approved_by="秘书处-张",
        )
        # 已迁出边界的保护地不能再申报
        with self.assertRaises(ValidationError):
            make_claim(self.service, "CLM-OLD", sites=("SITE-1",))
        # 新纳入的保护地可以申报
        claim = make_claim(self.service, "CLM-NEW", sites=("SITE-3",))
        self.assertEqual(claim.site_codes, ("SITE-3",))


if __name__ == "__main__":
    unittest.main()
