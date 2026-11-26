"""Adaptive controller contracts (pure data, M6).

A controller policy version is an immutable `ControllerPolicyDefinition`: typed parameters plus a
frozen estimator snapshot, hashed. Every decision stores the typed `DecisionFeatures` it used so a
decision can be recomputed from the stored record and the policy version alone.
"""

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from experience_engine.domain.decision import Strategy
from experience_engine.domain.outcome import OutcomeStatus

FEATURE_SCHEMA_VERSION = 1
CONTROLLER_KEY = "controller"  # feature_snapshot key holding `DecisionFeatures`
PLANNING_KEY = Strategy.GENERAL_PLANNING.value
Risk = Literal["low", "medium", "high"]
LearnedRisk = Literal["low", "medium"]  # `high` can never be a learned class (type-level)


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def action_key(strategy: Strategy, ref: str | None) -> str:
    """Stable id of one action: `GENERAL_PLANNING` or `<STRATEGY>:<capability id@version>`."""
    return strategy.value if ref is None else f"{strategy.value}:{ref}"


def parse_action_key(key: str) -> tuple[Strategy, str | None]:
    head, sep, ref = key.partition(":")
    return Strategy(head), (ref if sep else None)


def cell_key(family: str | None, risk: str, key: str) -> str:
    return f"{family or '-'}|{risk}|{key}"


def canonical_hash(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _default_learned() -> list[LearnedRisk]:
    return ["low"]


class ControllerKind(StrEnum):
    COST_AWARE = "COST_AWARE"  # deterministic, never explores
    BANDIT = "BANDIT"  # cost-aware + gated epsilon-greedy exploration


class ControllerParams(_F):
    """All tunables. Defaults are frozen in docs/experiments/adaptive-controller-design.md §7."""

    learned_risk_levels: list[LearnedRisk] = Field(default_factory=_default_learned)
    exploration_risk_levels: list[LearnedRisk] = Field(default_factory=list)
    exploration_families: list[str] = Field(default_factory=list)
    epsilon: float = Field(default=0.10, ge=0.0, le=0.5)
    n_min: int = Field(default=30, ge=1)
    min_total_verified: int = Field(default=100, ge=0)
    delta: dict[str, float] = Field(default_factory=lambda: {"low": 0.10, "medium": 0.05})
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    min_cost_saving: float = Field(default=0.10, ge=0.0, lt=1.0)
    cost_prior_weight: float = Field(default=5.0, ge=0.0)
    required_success: dict[str, float] = Field(default_factory=dict)

    @field_validator("learned_risk_levels", "exploration_risk_levels", "exploration_families")
    @classmethod
    def _sorted_unique(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    @model_validator(mode="after")
    def _consistent(self) -> "ControllerParams":
        if not set(self.exploration_risk_levels) <= set(self.learned_risk_levels):
            raise ValueError("exploration risk levels must be learned risk levels")
        for level in self.learned_risk_levels:
            if level not in self.delta:
                raise ValueError(f"missing non-inferiority margin for risk level {level!r}")
        if any(not 0.0 <= d <= 1.0 for d in self.delta.values()):
            raise ValueError("non-inferiority margins must lie in [0, 1]")
        if any(not 0.0 <= r <= 1.0 for r in self.required_success.values()):
            raise ValueError("required success thresholds must lie in [0, 1]")
        return self


class CellStats(_F):
    """Verified outcomes of one action in one (family, risk) context. UNKNOWN is never counted."""

    n: int = Field(ge=0)
    successes: int = Field(ge=0)
    token_sum: int = Field(ge=0)

    @model_validator(mode="after")
    def _ok(self) -> "CellStats":
        if self.successes > self.n:
            raise ValueError("successes cannot exceed trials")
        return self


class CalibrationSummary(_F):
    n: int
    brier: float | None
    brier_skill: float | None
    ece: float | None
    cost_bias: float | None
    cost_mape: float | None
    ok: bool
    reasons: list[str] = Field(default_factory=list)


class TrainingProvenance(_F):
    n_records: int
    n_verified: int
    n_unknown_excluded: int
    n_unacted_excluded: int
    first_decision_id: str | None
    last_decision_id: str | None
    source_hash: str  # hash of the ordered (decision id, action, status, tokens) used


class EstimatorSnapshot(_F):
    tenant_id: str
    cells: dict[str, CellStats] = Field(default_factory=dict)
    n_verified: int = 0
    calibration: CalibrationSummary | None = None
    provenance: TrainingProvenance | None = None

    @property
    def calibration_ok(self) -> bool:
        return self.calibration is not None and self.calibration.ok

    def snapshot_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class ControllerPolicyDefinition(_F):
    """An immutable controller policy version (params + frozen estimator)."""

    policy_id: str
    version: int = Field(ge=1)
    kind: ControllerKind
    params: ControllerParams
    snapshot: EstimatorSnapshot

    @property
    def tenant_id(self) -> str:
        return self.snapshot.tenant_id

    @property
    def policy_version(self) -> str:
        return f"{self.policy_id}/{self.version}"

    def definition_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class CandidateFeature(_F):
    key: str
    strategy: Strategy
    ref: str | None = None
    similarity: float = 0.0


class EvidenceView(_F):
    """The cell statistics the decision actually used (so a decision is self-contained)."""

    key: str
    n: int
    successes: int
    token_sum: int


class DecisionFeatures(_F):
    schema_version: int = FEATURE_SCHEMA_VERSION
    controller_kind: ControllerKind
    task_family: str | None
    risk_level: Risk
    n_allowed_tools: int
    tenant_id: str
    actions: list[CandidateFeature] = Field(min_length=1)  # the feasible set F(x)
    default_key: str  # the rule baseline's choice
    evidence: list[EvidenceView] = Field(default_factory=list)
    snapshot_hash: str
    calibration_ok: bool = False
    n_verified_total: int = 0
    protected: bool = False
    tenant_mismatch: bool = False
    u: float | None = None  # reproducible uniform draw (exploration decision)


class DecisionOutcomeRecord(_F):
    """One acted decision joined with its verified outcome: the training/replay unit."""

    decision_id: str
    run_id: str
    tenant_id: str
    policy_version: str
    acted: bool = True
    selection_probability: float | None
    family: str | None
    risk_level: Risk
    action: str  # action key actually executed
    status: OutcomeStatus
    tokens: int = Field(ge=0)
    n_allowed_tools: int = 0
    actions: list[CandidateFeature] = Field(default_factory=list)  # feasible set at decision time
    default_key: str = PLANNING_KEY  # the rule baseline's choice in that context
    propensities: dict[str, float] = Field(default_factory=dict)  # logged distribution, if known

    @property
    def known(self) -> bool:
        return self.status != OutcomeStatus.UNKNOWN

    @property
    def success(self) -> bool:
        return self.status == OutcomeStatus.VERIFIED_SUCCESS
