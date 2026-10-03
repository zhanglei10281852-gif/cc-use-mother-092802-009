"""场景二：同一片保护地 / 同一组受益社区 / 同一份生态调查的重复计量拦截。"""

import unittest

from _support import build_service, make_project, simple_claim
from biodiversity_review.contracts import ConflictKind, ConflictStatus, SubmissionStatus
from biodiversity_review.errors import ConflictError


def approve_ready(svc, submission_id, *, reviewer="expert", actor="secretariat", milestone=None):
    """让一份申报走完专家建议批准，再由秘书处尝试批准。"""
    svc.submit_expert_review(submission_id, reviewer=reviewer,
                             decision="recommend_approval", note="通过")
    return svc.approve_submission(submission_id, actor=actor)


class DuplicateDetectionTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        # 两个国家的两个项目，保护地 site-a 被同时列入各自边界
        make_project(self.svc, "p-np", "NP", ["site-a", "site-b"])
        make_project(self.svc, "p-br", "BR", ["site-x", "site-a"])
        for pid, ms in (("p-np", "m1-inventory"), ("p-br", "baseline")):
            self.svc.record_milestone(pid, milestone=ms, actor="officer",
                                      achieved_on="2026-03-01")

    def test_independent_outcomes_pass(self):
        """完全不同的保护地、社区、调查：无冲突，可顺利批准。"""
        s1 = self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-ind-1",
            milestone_code="m1-inventory",
            claims=[simple_claim("c-ind-1", outcome="habitat", group="comm-alpha",
                                 digest="sha256:survey-alpha", sites=["site-a"])],
        )
        s2 = self.svc.submit(
            project_id="p-br", submitted_by="ngo-b", submission_id="sub-ind-2",
            milestone_code="baseline",
            claims=[simple_claim("c-ind-2", outcome="forest", group="comm-beta",
                                 digest="sha256:survey-beta", sites=["site-x"])],
        )
        self.assertEqual(s1["conflicts"], [])
        self.assertEqual(s2["conflicts"], [])
        approve_ready(self.svc, "sub-ind-1")
        approve_ready(self.svc, "sub-ind-2")
        self.assertEqual(self.svc.get_submission("sub-ind-2")["status"],
                         SubmissionStatus.APPROVED.value)

    def test_shared_survey_is_flagged_and_blocks_approval(self):
        """两个申报方把同一份生态调查（相同摘要）重复算作各自成果。"""
        self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-dup-1",
            claims=[simple_claim("c-dup-1", group="comm-a", digest="sha256:SAME-SURVEY",
                                 sites=["site-b"])],
        )
        s2 = self.svc.submit(
            project_id="p-br", submitted_by="ngo-b", submission_id="sub-dup-2",
            claims=[simple_claim("c-dup-2", outcome="forest", group="comm-b",
                                 digest="sha256:same-survey",
                                 sites=["site-x"])],  # 大小写归一化后仍相同
        )
        kinds = {c["kind"] for c in s2["conflicts"]}
        self.assertIn(ConflictKind.SHARED_SURVEY.value, kinds)
        conflict = next(c for c in s2["conflicts"]
                        if c["kind"] == ConflictKind.SHARED_SURVEY.value)
        self.assertEqual(conflict["status"], ConflictStatus.OPEN.value)
        self.assertEqual(conflict["claims"][0]["claim_id"], "c-dup-2")

        # 专家建议批准，但秘书处批准被冲突拦截
        self.svc.submit_expert_review("sub-dup-2", reviewer="expert",
                                      decision="recommend_approval")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.approve_submission("sub-dup-2", actor="secretariat")
        self.assertIn("重复计量冲突", str(ctx.exception))

    def test_shared_site_and_beneficiary_conflicts(self):
        """同一片保护地与同一组受益社区各自产生独立冲突记录。"""
        self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-mix-1",
            claims=[simple_claim("c-mix-1", group="comm-shared",
                                 digest="sha256:surv-1", sites=["site-a"])],
        )
        s2 = self.svc.submit(
            project_id="p-br", submitted_by="ngo-b", submission_id="sub-mix-2",
            claims=[simple_claim("c-mix-2", outcome="forest", group="COMM-SHARED",  # 归一化后相同
                                 digest="sha256:surv-2", sites=["site-a"])],
        )
        kinds = {c["kind"] for c in s2["conflicts"]}
        self.assertEqual(
            kinds,
            {ConflictKind.SHARED_SITE.value, ConflictKind.SHARED_BENEFICIARY.value},
        )
        self.assertNotIn(ConflictKind.SHARED_SURVEY.value, kinds)
        detail = next(c for c in s2["conflicts"]
                      if c["kind"] == ConflictKind.SHARED_SITE.value)["detail"]
        self.assertEqual(detail["site_codes"], ["site-a"])

    def test_conflict_resolved_as_legitimate_sharing_allows_approval(self):
        """秘书处裁决为联合资助等合理共享后，双方均可批准。"""
        s1 = self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-joint-1",
            milestone_code="m1-inventory",
            claims=[simple_claim("c-joint-1", group="comm-joint",
                                 digest="sha256:joint-survey")],
        )
        s2 = self.svc.submit(
            project_id="p-br", submitted_by="ngo-b", submission_id="sub-joint-2",
            milestone_code="baseline",
            claims=[simple_claim("c-joint-2", outcome="forest", group="comm-joint",
                                 digest="sha256:joint-survey")],
        )
        # 同一冲突会出现在双方视图中，裁决只做一次
        conflict_id = s2["conflicts"][0]["conflict_id"]
        with self.assertRaises(Exception):
            # 裁决必须写理由
            self.svc.resolve_conflict(conflict_id, actor="secretariat",
                                      resolution_status="resolved", rationale=" ")
        self.svc.resolve_conflict(
            conflict_id, actor="secretariat",
            resolution_status="resolved",
            rationale="该调查由双边联合资助，成本与成果已按 50/50 拆分申报",
        )
        # 可能还有 site/beneficiary 冲突，逐一裁决为合理共享
        remaining = self.svc.list_conflicts(status="open")
        for c in remaining:
            self.svc.resolve_conflict(c["conflict_id"], actor="secretariat",
                                      resolution_status="resolved",
                                      rationale="联合项目，社区共管同一保护地")
        approve_ready(self.svc, "sub-joint-1")
        approve_ready(self.svc, "sub-joint-2")
        both_approved = all(
            s["status"] == SubmissionStatus.APPROVED.value
            for s in self.svc.list_submissions()
        )
        self.assertTrue(both_approved)

    def test_confirmed_duplicate_keeps_blocking(self):
        """裁决确认是重复申报：即使后来再次走专家流程，批准仍被拦截。"""
        self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-cd-1",
            claims=[simple_claim("c-cd-1", group="comm-cd", digest="sha256:dup")],
        )
        s2 = self.svc.submit(
            project_id="p-br", submitted_by="ngo-b", submission_id="sub-cd-2",
            claims=[simple_claim("c-cd-2", outcome="forest", group="comm-cd",
                                 digest="sha256:dup")],
        )
        for c in s2["conflicts"]:
            self.svc.resolve_conflict(
                c["conflict_id"], actor="secretariat",
                resolution_status="confirmed_duplicate",
                rationale="BR 方直接复用 NP 方调查，未实际开展独立工作",
            )
        self.svc.submit_expert_review("sub-cd-2", reviewer="expert",
                                      decision="recommend_approval")
        with self.assertRaises(ConflictError):
            self.svc.approve_submission("sub-cd-2", actor="secretariat")

    def test_withdrawn_submission_releases_its_dimensions(self):
        """撤回后其保护地/社区/调查维度释放，后续独立申报不再产生冲突。"""
        self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-rel-1",
            claims=[simple_claim("c-rel-1", group="comm-rel",
                                 digest="sha256:rel", sites=["site-b"])],
        )
        s2 = self.svc.submit(
            project_id="p-np", submitted_by="ngo-a2", submission_id="sub-rel-2",
            claims=[simple_claim("c-rel-2", group="comm-rel",
                                 digest="sha256:rel", sites=["site-b"])],
        )
        self.assertTrue(s2["conflicts"])
        self.svc.withdraw_submission("sub-rel-1", actor="ngo-a", reason="放弃申报")
        # 开放冲突随撤回自动关闭
        self.assertEqual(self.svc.list_conflicts(status="open"), [])

    def test_evidence_pack_aggregates_conflicts_per_project(self):
        self.svc.submit(
            project_id="p-np", submitted_by="ngo-a", submission_id="sub-pack-1",
            claims=[simple_claim("c-pack-1", group="comm-pack", digest="sha256:pack",
                                 sites=["site-b"])],
        )
        self.svc.submit(
            project_id="p-br", submitted_by="ngo-b", submission_id="sub-pack-2",
            claims=[simple_claim("c-pack-2", outcome="survey", group="comm-pack",
                                 digest="sha256:pack", sites=["site-x"])],
        )
        pack = self.svc.project_evidence_pack("p-np")
        self.assertEqual([s["submission_id"] for s in pack["submissions"]], ["sub-pack-1"])
        self.assertEqual(len(pack["conflicts"]), 2)  # 社区 + 调查（保护地不同：site-b vs site-x）
        kinds = {c["kind"] for c in pack["conflicts"]}
        self.assertEqual(kinds,
                         {ConflictKind.SHARED_BENEFICIARY.value, ConflictKind.SHARED_SURVEY.value})
        self.assertIn("boundary_revisions", pack["project"])


if __name__ == "__main__":
    unittest.main()
