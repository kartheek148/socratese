"""Evidence-quality contracts (M6.2): an explicit gate list, not a score."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.evaluation import ServingEvidence


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceKind(StrEnum):
    PROMOTION_EVALUATION = "PROMOTION_EVALUATION"  # the real evaluation workflow (production)
    EXPERIMENT_FILE = "EXPERIMENT_FILE"  # a recorded ee-exp / paired-run file (research)
    STRESS_FIXTURE = "STRESS_FIXTURE"  # hand-authored fixtures, never promoted (research)


class EvidenceAssessment(_F):
    """Why a paired prior may, or may not, seed a production policy."""

    cell_key: str
    eligible: bool  # every gate passed: production-eligible
    reasons: list[str] = Field(default_factory=list)  # one entry per failed gate
    verified_pair_count: int
    n_unknown_dropped: int = 0
    n_tied_dropped: int = 0
    confidence: dict[str, float] = Field(default_factory=dict)  # paired bound of alt - default
    compatibility: dict[str, bool] = Field(default_factory=dict)
    freshness: dict[str, Any] = Field(default_factory=dict)
    drift_state: str = "NORMAL"
    cost_measurement: dict[str, float] = Field(default_factory=dict)
    provenance: dict[str, str | None] = Field(default_factory=dict)
    overrides: list[str] = Field(default_factory=list)  # explicit overrides taken (audited)


@dataclass(frozen=True)
class EvaluationRecord:
    """The columns of one stored evaluation that the prior builder reads."""

    evaluation_id: str
    tenant_id: str
    capability_id: str
    version: int
    verdict: str
    risk_class: str
    runtime_version: str
    verifier_version: str
    environment_version: str
    evaluator_version: str
    report_sha256: str
    completed_at: datetime
    model_settings: dict[str, Any]
    dataset_versions: dict[str, dict[str, str]]
    metrics: dict[str, Any]
    seeds: list[int] = field(default_factory=list)

    @property
    def serving(self) -> ServingEvidence | None:
        raw = self.metrics.get("serving_evidence")
        return ServingEvidence.model_validate(raw) if raw else None
