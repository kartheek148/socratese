"""Adaptive controller contracts (pure data, M6).

A controller policy version is an immutable `ControllerPolicyDefinition`: typed parameters plus a
frozen estimator snapshot, hashed. Every decision stores the typed `DecisionFeatures` it used so a
decision can be recomputed from the stored record and the policy version alone.
"""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from experience_engine.domain.decision import Strategy
from experience_engine.domain.drift import DriftParams, DriftView
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


def paired_key(family: str | None, risk: str, default_key: str, alt_key: str) -> str:
    """Key of a paired prior cell: the rule default and one alternative in one context."""
    return f"{family or '-'}|{risk}|{default_key}>{alt_key}"


def canonical_hash(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _default_learned() -> list[LearnedRisk]:
    return ["low"]


class ControllerKind(StrEnum):
    COST_AWARE = "COST_AWARE"  # deterministic, never explores
    BANDIT = "BANDIT"  # cost-aware + gated epsilon-greedy exploration
    EVIDENCE_DRIVEN = "EVIDENCE_DRIVEN"  # explores only while non-inferiority is unresolved (M6.1)


class EvidenceParams(_F):
    """M6.1 evidence module (frozen defaults: evidence-driven-controller-design.md section 6)."""

    n_min_pairs: int = Field(default=30, ge=2)  # paired tasks needed before a prior cell is used
    drift_min_n: int = Field(default=30, ge=1)  # live outcomes needed before the drift guard acts
    explore_target_cap: int = Field(
        default=400, ge=1
    )  # per-alternative cap of the exploration target
    # M6.2: the offline gate FAILs priors that are not production-eligible (omitted when False)
    require_production_eligible: bool = False

    @model_serializer(mode="wrap")
    def _omit_default_eligibility(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.require_production_eligible:
            data.pop("require_production_eligible", None)
        return data


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
    # None = M6 behaviour (priors ignored, constant-epsilon bandit). Omitted from the serialised
    # form when None, so every M6 definition keeps its hash.
    evidence: EvidenceParams | None = None
    # M6.2 windowed drift monitor (needs the evidence module); omitted when None
    drift: DriftParams | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_evidence(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.evidence is None:
            data.pop("evidence", None)
        if self.drift is None:
            data.pop("drift", None)
        return data

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
        if self.evidence is not None and self.required_success:
            raise ValueError("required_success is not supported with the evidence module")
        if self.drift is not None and self.evidence is None:
            raise ValueError("the drift monitor needs the evidence module")
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


class PairedCell(_F):
    """Held-out outcomes of the rule default and ONE alternative on the same tasks (2x2 table).

    The unit is the final production outcome (planner fallback included). UNKNOWN pairs are never
    counted. Tokens are the sums over the same tasks for each action.
    """

    n: int = Field(ge=0)
    both: int = Field(ge=0)
    default_only: int = Field(ge=0)
    alt_only: int = Field(ge=0)
    neither: int = Field(ge=0)
    default_tokens: int = Field(ge=0)
    alt_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _table(self) -> "PairedCell":
        if self.both + self.default_only + self.alt_only + self.neither != self.n:
            raise ValueError("paired table must sum to n")
        return self

    @property
    def default_rate(self) -> float:
        return (self.both + self.default_only) / self.n if self.n else 0.0

    @property
    def alt_rate(self) -> float:
        return (self.both + self.alt_only) / self.n if self.n else 0.0


class PairedSource(_F):
    """Provenance of one paired validation source (what the prior was computed from)."""

    name: str
    arms: list[str]
    splits: list[str]
    dataset_hashes: dict[str, str]
    model: str
    runtime_version: str
    verifier_version: str
    n_pairs: int = Field(ge=0)
    n_unknown_dropped: int = Field(ge=0)
    n_tied_dropped: int = Field(ge=0)
    tasks_sha256: str
    validation_tokens: int = Field(
        ge=0
    )  # tokens of both actions over the used pairs: learning cost
    capabilities: list[str] = Field(default_factory=list)
    # M6.2 provenance and eligibility (all omitted from the serialised form when unset, so the
    # hashes of M6.1 definitions are unchanged)
    evidence_kind: str | None = None
    production_eligible: bool | None = None
    tenant_id: str | None = None
    risk_class: str | None = None
    evaluation_id: str | None = None
    report_sha256: str | None = None
    evaluation_config_hash: str | None = None
    environment_version: str | None = None
    recorded_at: datetime | None = None
    n_missing_cost: int | None = None
    overrides: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_unset_provenance(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in _PROVENANCE_OPTIONAL:
            if data.get(key) in (None, []):
                data.pop(key, None)
        return data


_PROVENANCE_OPTIONAL = (
    "evidence_kind",
    "production_eligible",
    "tenant_id",
    "risk_class",
    "evaluation_id",
    "report_sha256",
    "evaluation_config_hash",
    "environment_version",
    "recorded_at",
    "n_missing_cost",
    "overrides",
)


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
    # M6.1 paired validation priors; omitted from the serialised form when empty (M6 hashes)
    paired: dict[str, PairedCell] = Field(default_factory=dict)
    prior_sources: list[PairedSource] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_empty_priors(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.paired:
            data.pop("paired", None)
        if not self.prior_sources:
            data.pop("prior_sources", None)
        return data

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

    @model_validator(mode="after")
    def _evidence_consistent(self) -> "ControllerPolicyDefinition":
        if self.kind == ControllerKind.EVIDENCE_DRIVEN and self.params.evidence is None:
            raise ValueError("EVIDENCE_DRIVEN policies need the evidence parameters")
        if self.snapshot.paired and self.params.evidence is None:
            raise ValueError("paired priors need the evidence parameters")
        return self

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


class PairedView(_F):
    """The paired prior cell a decision read (the default vs alternative `key`)."""

    key: str
    n: int
    both: int
    default_only: int
    alt_only: int
    neither: int
    default_tokens: int
    alt_tokens: int


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
    paired: list[PairedView] = Field(default_factory=list)  # M6.1 paired priors read
    evidence_module: bool = False  # M6.1 decision rules apply (False: exact M6 behaviour)
    drift: list[DriftView] = Field(default_factory=list)  # M6.2 drift overlay read (ENFORCE)


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
    # M6.2 provenance of the observation (not used by estimators; read by the drift monitor)
    observed_at: datetime | None = None
    runtime_version: str | None = None
    verifier_version: str | None = None
    latency_ms: int | None = None

    @property
    def known(self) -> bool:
        return self.status != OutcomeStatus.UNKNOWN

    @property
    def success(self) -> bool:
        return self.status == OutcomeStatus.VERIFIED_SUCCESS
