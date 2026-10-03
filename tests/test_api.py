"""场景五：审查人员通过 HTTP API 按项目查看证据、冲突与批次版本。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from biodiversity_review.api import create_server  # noqa: E402
from _support import build_service  # noqa: E402


class ApiClient:
    def __init__(self, base_url: str):
        self.base = base_url

    def request(self, method: str, path: str, body=None, actor="secretariat"):
        data = None
        headers = {"X-Actor": actor}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = build_service()
        cls.server = create_server("127.0.0.1", 0, service=cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.server.server_address[1]}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_01_full_review_journey_over_http(self):
        # 两个项目
        status, _ = self.api.request("POST", "/projects", {
            "project_id": "p-np", "country_code": "np",
            "site_codes": ["site-a", "site-b"], "effective_from": "2026-01-01",
        })
        self.assertEqual(status, 201)
        status, _ = self.api.request("POST", "/projects", {
            "project_id": "p-br", "country_code": "br",
            "site_codes": ["site-x"], "effective_from": "2026-02-01",
        })
        self.assertEqual(status, 201)

        # 里程碑
        status, _ = self.api.request("POST", "/projects/p-np/milestones",
                                     {"milestone": "m1-inventory"})
        self.assertEqual(status, 201)
        status, body = self.api.request("POST", "/projects/p-np/milestones",
                                        {"milestone": "baseline"})
        self.assertEqual(status, 422)  # 别国里程碑被拒

        # 两份申报：同一份调查 + 同一受益社区 → 冲突
        claim = lambda cid, group, digest: {
            "claim_id": cid, "outcome_code": "forest",
            "beneficiary_group": group, "evidence_digest": digest, "measure": 5.0,
        }
        status, sub1 = self.api.request("POST", "/projects/p-np/submissions", {
            "submission_id": "sub-1", "submitted_by": "ngo-a", "milestone_code": "m1-inventory",
            "claims": [claim("c-1", "comm-s", "sha256:shared-survey")],
            "attachments": [{"attachment_id": "att-1", "filename": "s.pdf",
                             "sha256": "sha256:shared-survey", "summary": "联合调查"}],
        })
        self.assertEqual(status, 201)
        self.assertEqual(sub1["conflicts"], [])

        status, sub2 = self.api.request("POST", "/projects/p-br/submissions", {
            "submission_id": "sub-2", "submitted_by": "ngo-b",
            "claims": [claim("c-2", "comm-s", "sha256:shared-survey")],
        })
        self.assertEqual(status, 201)
        # 保护地不重叠 → 两个冲突：受益社区 + 调查
        self.assertEqual({c["kind"] for c in sub2["conflicts"]},
                         {"shared_beneficiary", "shared_survey"})

        # 专家建议批准，秘书处批准被拦截
        status, _ = self.api.request("POST", "/submissions/sub-2/reviews", {
            "reviewer": "expert-li", "decision": "recommend_approval",
            "checked_attachment_ids": [],
        })
        self.assertEqual(status, 201)
        status, err = self.api.request("POST", "/submissions/sub-2/approve", {})
        self.assertEqual(status, 409)
        self.assertIn("重复计量冲突", err["message"])

        # 秘书处裁决冲突为确认重复 → 仍拦截；改判独立成果不受影响
        status, conflicts = self.api.request("GET", "/conflicts?status=open")
        self.assertEqual(status, 200)
        self.assertEqual(len(conflicts["conflicts"]), 2)
        for c in conflicts["conflicts"]:
            status, _ = self.api.request("POST",
                                         f"/conflicts/{c['conflict_id']}/resolution",
                                         {"status": "resolved",
                                          "rationale": "联合资助，已拆分计量"})
            self.assertEqual(status, 200)

        # sub-1 需要专家复核后批准；sub-2 未声明里程碑因此也可批准
        status, _ = self.api.request("POST", "/submissions/sub-1/reviews", {
            "reviewer": "expert-li", "decision": "recommend_approval",
        })
        self.assertEqual(status, 201)
        status, approved1 = self.api.request("POST", "/submissions/sub-1/approve", {})
        self.assertEqual(status, 200)
        status, approved2 = self.api.request("POST", "/submissions/sub-2/approve", {})
        self.assertEqual(status, 200)

        # 证据包：审查人员按项目查看证据附件、申报与冲突关系
        status, pack = self.api.request("GET", "/projects/p-np/evidence-pack")
        self.assertEqual(status, 200)
        self.assertEqual(pack["project"]["boundary_revisions"][0]["rationale"],
                         "立项原始边界")
        self.assertEqual(pack["submissions"][0]["attachments"][0]["sha256"],
                         "sha256:shared-survey")
        self.assertTrue(pack["audit_trail"])

        # 发布 v1 → 撤销 c-2 → 发布 v2
        status, v1 = self.api.request("POST", "/batches/B-2026/compile",
                                      {"label": "v1"}, actor="secretariat")
        self.assertEqual(status, 201)
        self.assertEqual(v1["totals"]["overall"]["claim_count"], 2)

        status, revoke = self.api.request(
            "POST", "/submissions/sub-2/claims/c-2/revoke",
            {"reason": "调查样方无法复核"}, actor="secretariat",
        )
        self.assertEqual(status, 200)
        self.assertEqual(revoke["affected_publications"][0]["version"], 1)

        status, v2 = self.api.request("POST", "/batches/B-2026/compile", {"label": "v2"})
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["changes_from_previous"]["removed"][0]["reason"], "revoked")

        status, impacts = self.api.request("GET", "/batches/B-2026/impacts")
        self.assertEqual(status, 200)
        self.assertEqual(impacts["impacts"][0]["status"], "corrected")
        self.assertEqual(impacts["impacts"][0]["corrected_in_version"], 2)

        # 边界修订后历史仍可取
        status, rev = self.api.request("POST", "/projects/p-br/boundary-revisions", {
            "site_codes": ["site-x", "site-y"],
            "rationale": "新增廊道保护地 site-y，经董事会核准",
        })
        self.assertEqual(status, 201)
        status, history = self.api.request("GET", "/projects/p-br/boundaries")
        self.assertEqual([r["revision"] for r in history["revisions"]], [0, 1])

        # 审计端点可过滤到具体申报；撤销动作记在成果实体上
        status, audit = self.api.request(
            "GET", "/audit?entity_type=submission&entity_id=sub-2")
        actions = [e["action"] for e in audit["events"]]
        self.assertEqual(actions[0], "submitted")
        status, claim_audit = self.api.request(
            "GET", "/audit?entity_type=claim&entity_id=c-2")
        self.assertIn("claim_revoked", [e["action"] for e in claim_audit["events"]])

    def test_404_and_bad_json_are_clean(self):
        status, body = self.api.request("GET", "/projects/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NotFound")
        # 缺指标定义 → 422
        self.api.request("POST", "/projects", {
            "project_id": "p-x", "country_code": "br", "site_codes": ["s"]})
        status, body = self.api.request("POST", "/projects/p-x/submissions", {
            "submitted_by": "ngo",
            "claims": [{"claim_id": "cx", "outcome_code": "nope",
                        "beneficiary_group": "g", "evidence_digest": "sha256:1"}],
        })
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
