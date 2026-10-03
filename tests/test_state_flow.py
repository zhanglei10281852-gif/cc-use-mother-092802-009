"""场景一：申报 → 要求补充 → 补充材料 → 专家复核 → 批准/驳回/撤回的状态流。"""

import unittest

from _support import build_service, make_project, simple_claim
from biodiversity_review.contracts import SubmissionStatus
from biodiversity_review.errors import ConflictError, InvalidTransition


class StateFlowTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        make_project(self.svc, "p-np", "NP", ["site-a", "site-b"])
        # 里程碑先完成，便于批准
        self.svc.record_milestone(
            "p-np", milestone="m1-inventory", achieved_on="2026-03-01",
            note="清查完成", actor="officer-np",
        )

    def _submit(self, sid="sub-1", digest="sha256:aaa", group="comm-a"):
        return self.svc.submit(
            project_id="p-np",
            submitted_by="ngo-nepal",
            submission_id=sid,
            milestone_code="m1-inventory",
            claims=[simple_claim("c-1", digest=digest, group=group)],
            attachments=[
                {"attachment_id": "att-1", "filename": "survey.pdf",
                 "sha256": digest, "summary": "首次生态调查"}
            ],
        )

    def test_full_flow_submit_supplement_review_approve(self):
        sub = self._submit()
        self.assertEqual(sub["status"], SubmissionStatus.SUBMITTED.value)
        # 未声明的指标不得申报
        with self.assertRaises(Exception):
            self.svc.submit(
                project_id="p-np", submitted_by="ngo-nepal",
                claims=[simple_claim("c-x", outcome="wetland")],
            )

        # 专家首轮：要求补充
        self.svc.submit_expert_review(
            "sub-1", reviewer="expert-li", decision="request_supplement",
            note="缺少社区知情同意",
        )
        sub = self.svc.get_submission("sub-1")
        self.assertEqual(sub["status"], SubmissionStatus.AWAITING_SUPPLEMENT.value)

        # 未补交前不能再次复核（必须先补材料）
        with self.assertRaises(InvalidTransition):
            self.svc.submit_expert_review(
                "sub-1", reviewer="expert-li", decision="recommend_approval")

        # 申报方补交
        self.svc.add_supplement(
            "sub-1", submitted_by="ngo-nepal", note="补交同意书",
            attachments=[{"attachment_id": "att-2", "filename": "consent.pdf",
                          "sha256": "sha256:bbb", "summary": "社区会议记录与同意书"}],
        )
        sub = self.svc.get_submission("sub-1")
        self.assertEqual(sub["status"], SubmissionStatus.UNDER_REVIEW.value)
        self.assertEqual([s["supplement_id"] for s in sub["supplements"]],
                         sub["supplement_ids"])
        self.assertEqual({a["attachment_id"] for a in sub["attachments"]},
                         {"att-1", "att-2"})

        # 专家二轮：建议批准；秘书处批准
        self.svc.submit_expert_review(
            "sub-1", reviewer="expert-li", decision="recommend_approval",
            note="材料齐备", checked_attachment_ids=["att-1", "att-2"],
        )
        approved = self.svc.approve_submission("sub-1", actor="secretariat")
        self.assertEqual(approved["status"], SubmissionStatus.APPROVED.value)
        self.assertEqual(approved["claims"][0]["status"], "approved")

        # 审计轨迹完整记录每一步
        actions = [e.action for e in self.svc.audit.for_entity("submission", "sub-1")]
        self.assertEqual(
            actions,
            ["submitted", "expert_review", "supplement_added", "expert_review", "approved"],
        )

    def test_cannot_approve_without_expert_recommendation(self):
        self._submit(sid="sub-solo", digest="sha256:z1", group="comm-z")
        with self.assertRaises(InvalidTransition):
            self.svc.approve_submission("sub-solo", actor="secretariat")

    def test_withdraw_before_approval_is_terminal_and_claim_marked(self):
        self._submit(sid="sub-w", digest="sha256:w1", group="comm-w")
        self.svc.withdraw_submission("sub-w", actor="ngo-nepal", reason="申报方发现数据错误")
        sub = self.svc.get_submission("sub-w")
        self.assertEqual(sub["status"], SubmissionStatus.WITHDRAWN.value)
        self.assertEqual(sub["claims"][0]["status"], "withdrawn")
        # 终态后不可撤回或批准
        with self.assertRaises(InvalidTransition):
            self.svc.withdraw_submission("sub-w", actor="ngo-nepal")
        # 审计保留撤回原因
        event = [e for e in self.svc.audit.for_entity("submission", "sub-w")
                 if e.action == "withdrawn"][0]
        self.assertEqual(event.detail["reason"], "申报方发现数据错误")

    def test_reject_requires_negative_review(self):
        self._submit(sid="sub-r", digest="sha256:r1", group="comm-r")
        with self.assertRaises(InvalidTransition):
            self.svc.reject_submission("sub-r", actor="secretariat")
        self.svc.submit_expert_review(
            "sub-r", reviewer="expert-li", decision="reject", note="证据不成立")
        rejected = self.svc.reject_submission("sub-r", actor="secretariat", reason="证据不足")
        self.assertEqual(rejected["status"], SubmissionStatus.REJECTED.value)


if __name__ == "__main__":
    unittest.main()
