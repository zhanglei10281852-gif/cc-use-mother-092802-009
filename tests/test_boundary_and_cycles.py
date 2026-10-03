"""场景三：边界变更保留历史版本与核准理由；各国周期指标/里程碑互不混用。"""

import unittest

from _support import build_service, make_project
from biodiversity_review.errors import ValidationError


class BoundaryRevisionTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        make_project(self.svc, "p-np", "NP", ["site-a", "site-b"])

    def test_original_boundary_kept_with_rationale(self):
        original = self.svc.current_boundary("p-np")
        self.assertEqual(original["revision"], 0)
        self.assertEqual(original["site_codes"], ["site-a", "site-b"])
        self.assertEqual(original["rationale"], "立项原始边界")

        rev = self.svc.revise_boundary(
            "p-np",
            site_codes=["site-a", "site-c"],
            rationale="site-b 划入新建国家公园，经双边备忘录核准移出，新增 site-c",
            effective_from="2026-07-01",
            actor="director-chen",
        )
        self.assertEqual(rev["revision"], 1)

        history = self.svc.boundary_history("p-np")
        self.assertEqual(len(history), 2)
        # 原边界原样保留
        self.assertEqual(history[0]["site_codes"], ["site-a", "site-b"])
        self.assertEqual(history[0]["approved_by"], "secretariat")
        # 新版本记录核准理由、核准人与生效日
        self.assertEqual(history[1]["site_codes"], ["site-a", "site-c"])
        self.assertEqual(history[1]["approved_by"], "director-chen")
        self.assertEqual(history[1]["effective_from"], "2026-07-01")
        self.assertIn("双边备忘录", history[1]["rationale"])

        # 审计可回溯
        event = [e for e in self.svc.audit.for_entity("project", "p-np")
                 if e.action == "boundary_revised"][0]
        self.assertEqual(event.detail["revision"], 1)

    def test_revision_requires_rationale_and_actual_change(self):
        with self.assertRaises(ValidationError):
            self.svc.revise_boundary("p-np", site_codes=["site-a", "site-b"],
                                     rationale="  ", actor="x")
        with self.assertRaises(ValidationError):
            self.svc.revise_boundary("p-np", site_codes=["site-a", "site-b"],
                                     rationale="没有变化", actor="x")

    def test_claim_sites_must_lie_within_current_boundary(self):
        with self.assertRaises(ValidationError):
            self.svc.submit(
                project_id="p-np", submitted_by="ngo",
                claims=[{
                    "claim_id": "c-out", "outcome_code": "habitat",
                    "beneficiary_group": "g", "evidence_digest": "sha256:1",
                    "site_codes": ["site-zzz"],
                }],
            )

    def test_new_claims_snapshot_new_boundary_after_revision(self):
        self.svc.revise_boundary(
            "p-np", site_codes=["site-c"], rationale="项目整体迁移至新保护地",
            actor="director-chen",
        )
        sub = self.svc.submit(
            project_id="p-np", submitted_by="ngo",
            claims=[{
                "claim_id": "c-new", "outcome_code": "habitat",
                "beneficiary_group": "g", "evidence_digest": "sha256:1",
            }],
        )
        self.assertEqual(sub["claims"][0]["site_codes"], ["site-c"])
        self.assertEqual(sub["claims"][0]["boundary_revision_at_submission"], 1)


class CountryCycleTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_milestones_and_indicators_are_country_specific(self):
        make_project(self.svc, "p-np", "NP", ["site-a"])
        # BR 的里程碑不能记在 NP 项目上
        with self.assertRaises(ValidationError):
            self.svc.record_milestone("p-np", milestone="baseline", actor="x")
        # NP 自己的里程碑可以
        self.svc.record_milestone("p-np", milestone="m1-inventory", actor="x")

        make_project(self.svc, "p-br", "BR", ["site-y"])
        with self.assertRaises(ValidationError):
            self.svc.submit(
                project_id="p-br", submitted_by="ngo",
                claims=[{
                    "claim_id": "c-bad", "outcome_code": "habitat",  # NP 指标
                    "beneficiary_group": "g", "evidence_digest": "sha256:1",
                }],
            )
        # BR 自己的指标 survey 可用
        sub = self.svc.submit(
            project_id="p-br", submitted_by="ngo",
            claims=[{
                "claim_id": "c-ok", "outcome_code": "survey",
                "beneficiary_group": "g", "evidence_digest": "sha256:1",
            }],
        )
        self.assertEqual(sub["country_code"], "BR")

    def test_project_must_belong_to_registered_cycle(self):
        from biodiversity_review.errors import NotFound
        with self.assertRaises(NotFound):
            make_project(self.svc, "p-zz", "ZZ", ["site"])


if __name__ == "__main__":
    unittest.main()
