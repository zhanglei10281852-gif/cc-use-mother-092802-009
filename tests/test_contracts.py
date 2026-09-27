import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from biodiversity_review.contracts import OutcomeClaim, ProjectBoundary


class BiodiversityContractTests(unittest.TestCase):
    def test_boundary_revision_is_explicit(self):
        boundary = ProjectBoundary("p-4", 3, "NP", ("site-a", "site-b"), date(2026, 1, 1))
        self.assertEqual(boundary.revision, 3)

    def test_outcome_claim_points_to_evidence_digest(self):
        claim = OutcomeClaim("c-1", "p-4", "habitat", "community-2", "sha256:abc")
        self.assertTrue(claim.evidence_digest.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
