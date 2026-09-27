"""项目边界与成果证据契约。"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ProjectBoundary:
    project_id: str
    revision: int
    country_code: str
    site_codes: tuple[str, ...]
    effective_from: date


@dataclass(frozen=True)
class OutcomeClaim:
    claim_id: str
    project_id: str
    outcome_code: str
    beneficiary_group: str
    evidence_digest: str
