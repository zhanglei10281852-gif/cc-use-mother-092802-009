"""场景四：成果撤销不抹除已发布统计；基金批次重新汇总产生新版本。"""

import unittest

from _support import build_service, make_project, simple_claim


class BatchVersioningTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        make_project(self.svc, "p-np", "NP", ["site-a"])
        make_project(self.svc, "p-br", "BR", ["site-x"])
        for pid, ms in (("p-np", "m1-inventory"), ("p-br", "baseline")):
            self.svc.record_milestone(pid, milestone=ms, actor="officer",
                                      achieved_on="2026-03-01")

    def _approved(self, sid, *, project, claim, milestone, group, digest, site,
                  measure=10.0, outcome="forest"):
        self.svc.submit(
            project_id=project, submitted_by="ngo", submission_id=sid,
            milestone_code=milestone,
            claims=[simple_claim(claim, outcome=outcome, group=group, digest=digest,
                                 sites=[site], measure=measure)],
        )
        self.svc.submit_expert_review(sid, reviewer="expert",
                                      decision="recommend_approval")
        self.svc.approve_submission(sid, actor="secretariat")

    def test_published_versions_survive_revocation_and_impact_is_locatable(self):
        self._approved("sub-1", project="p-np", claim="c-1", milestone="m1-inventory",
                       group="comm-a", digest="sha256:s1", site="site-a", measure=10.0)
        self._approved("sub-2", project="p-br", claim="c-2", milestone="baseline",
                       group="comm-b", digest="sha256:s2", site="site-x", measure=25.0)

        # 首版基金批次发布
        v1 = self.svc.compile_batch("B-2026Q1", actor="secretariat", label="第一季度发布")
        self.assertEqual(v1["version"], 1)
        self.assertIsNone(v1["supersedes"])
        self.assertEqual(v1["totals"]["overall"]["claim_count"], 2)
        self.assertEqual(v1["totals"]["overall"]["measure"], 35.0)
        v1_published_at = v1["compiled_at"]

        # c-2 的生态调查事后被认定造假 → 撤销（旧数据不删除）
        result = self.svc.revoke_claim(
            "sub-2", "c-2", actor="secretariat", reason="调查被认定伪造，原始样方不存在",
        )
        self.assertEqual(result["claim"]["status"], "revoked")
        # 撤销立即定位到受影响的已发布版本
        self.assertEqual(len(result["affected_publications"]), 1)
        affected = result["affected_publications"][0]
        self.assertEqual(affected["batch_id"], "B-2026Q1")
        self.assertEqual(affected["version"], 1)
        self.assertEqual(affected["claim_id"], "c-2")
        self.assertEqual(affected["status"], "affected")
        self.assertEqual(affected["published_at"], v1_published_at)

        # v1 仍然可取、统计原样保留（不抹除）
        frozen_v1 = self.svc.get_batch_version("B-2026Q1", 1)
        self.assertEqual(frozen_v1["totals"]["overall"]["measure"], 35.0)
        self.assertIn("c-2", {c["claim_id"] for c in frozen_v1["included_claims"]})

        # 重新汇总 → 追加 v2，而不是覆盖
        v2 = self.svc.compile_batch("B-2026Q1", actor="secretariat", label="第二季度发布")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes"], 1)
        self.assertEqual(v2["totals"]["overall"]["claim_count"], 1)
        self.assertEqual(v2["totals"]["overall"]["measure"], 10.0)
        self.assertEqual(v2["changes_from_previous"]["added"], [])
        self.assertEqual(
            v2["changes_from_previous"]["removed"],
            [{"claim_id": "c-2", "reason": "revoked"}],
        )

        # v1 与 v2 同时存在于批次历史
        batch = self.svc.get_batch("B-2026Q1")
        self.assertEqual(batch["current_version"], 2)
        self.assertEqual([v["version"] for v in batch["versions"]], [1, 2])
        self.assertEqual(batch["versions"][0]["totals"]["overall"]["measure"], 35.0)

        # 影响登记已被标记在 v2 更正
        impacts = self.svc.list_batch_impacts("B-2026Q1")
        self.assertEqual(len(impacts), 1)
        self.assertEqual(impacts[0]["status"], "corrected")
        self.assertEqual(impacts[0]["corrected_in_version"], 2)
        self.assertIn("伪造", impacts[0]["reason"])

    def test_versions_evolve_with_additions(self):
        self._approved("sub-1", project="p-np", claim="c-1", milestone="m1-inventory",
                       group="comm-a", digest="sha256:s1", site="site-a", measure=10.0)
        v1 = self.svc.compile_batch("B-growth", actor="secretariat")
        self.assertEqual(v1["totals"]["by_country"]["NP"]["by_outcome"]["forest"]["measure"], 10.0)

        # 第二批独立成果获批后重新汇总
        self._approved("sub-2", project="p-br", claim="c-2", milestone="baseline",
                       group="comm-b", digest="sha256:s2", site="site-x", measure=7.0)
        v2 = self.svc.compile_batch("B-growth", actor="secretariat")
        self.assertEqual(v2["changes_from_previous"]["added"], ["c-2"])
        self.assertEqual(v2["changes_from_previous"]["removed"], [])
        self.assertEqual(v2["totals"]["by_country"]["BR"]["by_outcome"]["forest"]["measure"], 7.0)
        self.assertEqual(v2["totals"]["overall"]["claim_count"], 2)

        # 空重汇总生成无变化新版本（每次编译都是一次留痕的发布动作）
        v3 = self.svc.compile_batch("B-growth", actor="secretariat")
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["changes_from_previous"]["added"], [])
        self.assertEqual(v3["changes_from_previous"]["removed"], [])

    def test_revocation_before_recompile_does_not_fabricate_history(self):
        """撤销发生在任何发布之前时，不应产生影响登记。"""
        self._approved("sub-1", project="p-np", claim="c-1", milestone="m1-inventory",
                       group="comm-a", digest="sha256:s1", site="site-a")
        self.svc.revoke_claim("sub-1", "c-1", actor="secretariat", reason="数据存疑")
        v1 = self.svc.compile_batch("B-fresh", actor="secretariat")
        self.assertEqual(v1["totals"]["overall"]["claim_count"], 0)
        self.assertEqual(self.svc.list_batch_impacts("B-fresh"), [])


if __name__ == "__main__":
    unittest.main()
