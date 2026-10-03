"""审查人员 API：按项目查看证据与冲突关系（服务层与 HTTP 层）。"""

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from biodiversity_review import ClaimStatus  # noqa: E402
from biodiversity_review.api import create_server  # noqa: E402
from helpers import make_claim, make_service, register_project  # noqa: E402


class ReviewQueryTests(unittest.TestCase):
    """服务层查询接口。"""

    def setUp(self):
        self.service = make_service()
        register_project(self.service, "P-A", applicant="OrgAlpha", sites=("SITE-1",))
        register_project(self.service, "P-B", applicant="OrgBeta", sites=("SITE-1", "SITE-9"))
        make_claim(self.service, "CLM-A1", project_id="P-A", sites=("SITE-1",))
        self.service.submit_claim("CLM-A1", actor="申报员-甲")
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-B",
            sites=("SITE-1",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        self.service.submit_claim("CLM-B1", actor="申报员-乙")  # 被拦截

    def test_project_evidence_lists_attachments(self):
        view = self.service.project_evidence("P-A")
        self.assertEqual(view["project_id"], "P-A")
        self.assertEqual(view["applicant_org"], "OrgAlpha")
        self.assertEqual(view["current_boundary_revision"], 1)
        self.assertEqual(len(view["claims"]), 1)
        claim_view = view["claims"][0]
        self.assertEqual(claim_view["claim_id"], "CLM-A1")
        self.assertEqual(claim_view["status"], ClaimStatus.SUBMITTED)
        self.assertEqual(claim_view["evidence"][0]["digest"], "sha256:ev-1")
        self.assertEqual(claim_view["survey_ids"], ["SVY-1"])

    def test_project_conflicts_show_cross_applicant_edges(self):
        view = self.service.project_conflicts("P-B")
        self.assertEqual(len(view["conflicts"]), 1)
        record = view["conflicts"][0]
        self.assertEqual(record.claim_id, "CLM-B1")
        self.assertEqual(record.other_project_id, "P-A")
        self.assertEqual(record.dimension.value, "protected_area")


class HttpReviewApiTests(unittest.TestCase):
    """HTTP 层：审查人员通过 REST 端点读取证据、冲突与审计。"""

    def setUp(self):
        self.service = make_service()
        register_project(self.service, "P-A", applicant="OrgAlpha", sites=("SITE-1",))
        register_project(self.service, "P-B", applicant="OrgBeta", sites=("SITE-1", "SITE-9"))
        make_claim(self.service, "CLM-A1", project_id="P-A", sites=("SITE-1",))
        self.service.submit_claim("CLM-A1", actor="申报员-甲")
        make_claim(
            self.service,
            "CLM-B1",
            project_id="P-B",
            sites=("SITE-1",),
            communities=("COM-9",),
            surveys=("SVY-9",),
        )
        self.service.submit_claim("CLM-B1", actor="申报员-乙")  # 被拦截
        self.server = create_server(self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, json.loads(body.decode("utf-8"))

    def test_conflicts_endpoint(self):
        status, payload = self._get("/projects/P-B/conflicts")
        self.assertEqual(status, 200)
        self.assertEqual(payload["project_id"], "P-B")
        conflict = payload["conflicts"][0]
        self.assertEqual(conflict["dimension"], "protected_area")
        self.assertEqual(conflict["overlapping_keys"], ["SITE-1"])
        self.assertEqual(conflict["other_project_id"], "P-A")
        self.assertEqual(conflict["status"], "open")

    def test_evidence_endpoint(self):
        status, payload = self._get("/projects/P-B/evidence")
        self.assertEqual(status, 200)
        self.assertEqual(payload["claims"][0]["status"], "blocked")
        self.assertEqual(
            payload["claims"][0]["evidence"][0]["digest"], "sha256:ev-1"
        )

    def test_audit_endpoint(self):
        status, payload = self._get("/claims/CLM-B1/audit")
        self.assertEqual(status, 200)
        actions = [event["action"] for event in payload]
        self.assertEqual(actions, ["claim.created", "claim.blocked"])
        self.assertEqual(payload[-1]["to_status"], "blocked")

    def test_snapshots_endpoint(self):
        status, payload = self._get("/batches/FUND-2026-Q3/snapshots")
        self.assertEqual(status, 200)
        self.assertEqual(payload, [])

    def test_unknown_resources_return_404(self):
        status, _ = self._get("/projects/P-404/evidence")
        self.assertEqual(status, 404)
        status, _ = self._get("/claims/CLM-404/audit")
        self.assertEqual(status, 404)
        status, _ = self._get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
