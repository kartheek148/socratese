"""Revalidation contracts (pure data, M6.3).

A *case* is opened for every quarantine episode of a monitored cell. It is a second, separate state
machine next to the M6.2 drift monitor (which is unchanged): quarantined experience is never
eligible for recovery automatically; it is retested by bounded, independently verified, out-of-band
trials, and restored only on explainable, persisted evidence. Design and frozen parameters:
docs/experiments/revalidation-design.md.
"""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

REVALIDATION_VERSION = "revalidation/1"


class _F(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QuarantineReason(StrEnum):
    """Why a cell was quarantined. A projection of the existing `DriftTrigger` (design §2.1)."""

    STATISTICAL_DRIFT = "STATISTICAL_DRIFT"
    PERFORMANCE_DEGRADATION = "PERFORMANCE_DEGRADATION"
    COST_DEGRADATION = "COST_DEGRADATION"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    STRUCTURAL_INVALIDATION = "STRUCTURAL_INVALIDATION"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    UNKNOWN = "UNKNOWN"


class RevalState(StrEnum):
    QUARANTINED = "QUARANTINED"
    REVALIDATION_PENDING = "REVALIDATION_PENDING"
    REVALIDATING = "REVALIDATING"
    RECOVERY_CANDIDATE = "RECOVERY_CANDIDATE"
    RECOVERED = "RECOVERED"
    EXPIRED = "EXPIRED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


TERMINAL_STATES = frozenset({RevalState.RECOVERED, RevalState.EXPIRED, RevalState.BUDGET_EXHAUSTED})


class RevalStrategy(StrEnum):
    FIXED = "FIXED"  # A: fixed dwell, fixed-size exact test
    EVIDENCE_THRESHOLD = "EVIDENCE_THRESHOLD"  # B: A's test, started on an evidence-readiness gate
    SEQUENTIAL = "SEQUENTIAL"  # C: one trial at a time, SPRT with the same error targets


class TrialVerdict(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"


class TrialStage(StrEnum):
    EVIDENCE = "EVIDENCE"
    CONFIRMATION = "CONFIRMATION"


class RejectReason(StrEnum):
    NOT_NEWER = "NOT_NEWER"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    DUPLICATE = "DUPLICATE"
    VERSION_MISMATCH = "VERSION_MISMATCH"


class RevalTrigger(StrEnum):
    CASE_OPENED = "CASE_OPENED"
    INELIGIBLE = "INELIGIBLE"
    READY = "READY"
    TRIALS_STARTED = "TRIALS_STARTED"
    CRITERIA_MET = "CRITERIA_MET"
    CRITERIA_NOT_MET = "CRITERIA_NOT_MET"
    CONFIRMED = "CONFIRMED"
    CONFIRMATION_FAILED = "CONFIRMATION_FAILED"
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
    LINEAGE_VIOLATION = "LINEAGE_VIOLATION"
    CYCLES_EXHAUSTED = "CYCLES_EXHAUSTED"
    TRIAL_CAP = "TRIAL_CAP"
    TOKEN_CAP = "TOKEN_CAP"
    LIFETIME_BUDGET = "LIFETIME_BUDGET"
    TTL_EXPIRED = "TTL_EXPIRED"
    CYCLE_EXPIRED = "CYCLE_EXPIRED"


class RevalidationParams(_F):
    """Revalidation configuration; part of the immutable policy definition (and its hash)."""

    strategy: RevalStrategy
    margin: float = Field(default=0.10, gt=0.0, lt=0.5)
    alpha: float = Field(default=0.05, gt=0.0, lt=0.5)
    alpha_severe: float = Field(default=0.025, gt=0.0, lt=0.5)
    severe_drop: float = Field(default=0.25, gt=0.0, lt=1.0)
    beta: float = Field(default=0.20, gt=0.0, lt=0.5)
    dwell: int = Field(default=50, ge=0)
    trials_per_round: int = Field(default=50, ge=1)
    round_hours: float = Field(default=10.0, gt=0.0)  # learning-plane round length (trial spacing)
    max_trials: int = Field(default=320, ge=10)
    max_unverified_fraction: float = Field(default=0.20, gt=0.0, lt=1.0)
    min_unverified_trials: int = Field(default=10, ge=1)
    max_cycles: int = Field(default=3, ge=1)
    max_cycle_hours: float = Field(default=400.0, gt=0.0)
    case_ttl_hours: float = Field(default=500.0, gt=0.0)
    cell_token_budget: int = Field(default=3_000_000, ge=1)
    min_pool: int = Field(default=50, ge=1)
    cost_guard: float = Field(default=1.15, ge=1.0)
    cost_futility_min_trials: int = Field(default=15, ge=2)
    cost_futility_confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    confirm_gap_hours: float = Field(default=20.0, ge=0.0)
    confirm_trials: int = Field(default=20, ge=0)
    confirm_alpha: float = Field(default=0.01, gt=0.0, lt=0.5)
    allowed_risks: tuple[str, ...] = ("low",)
    ready_default_min: int = Field(default=30, ge=1)
    ready_confidence: float = Field(default=0.90, gt=0.5, lt=1.0)
    lineage_mismatch_limit: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> "RevalidationParams":
        if self.alpha_severe > self.alpha:
            raise ValueError("alpha_severe must not be looser than alpha")
        if self.confirm_trials > self.max_trials:
            raise ValueError("confirm_trials cannot exceed max_trials")
        if not self.allowed_risks:
            raise ValueError("allowed_risks must not be empty")
        return self

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class ExecutorFacts(_F):
    """What the revalidation executor reports about itself at a round (persisted, so eligibility
    decisions replay)."""

    tenant_id: str
    supported: bool = True
    runtime_version: str | None = None
    verifier_version: str | None = None
    verification_available: bool = True
    pool: int = Field(default=0, ge=0)


class TrialKey(_F):
    cell_key: str
    episode: int = Field(ge=0)
    cycle: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    stage: TrialStage = TrialStage.EVIDENCE

    def ident(self) -> str:
        return f"{self.cell_key}|e{self.episode}|c{self.cycle}|{self.stage.value[0]}{self.ordinal}"


class TrialResult(_F):
    """One executed, independently verified (or not) test. Written only by the executor port."""

    verdict: TrialVerdict
    tokens: int = Field(ge=0)
    task_ref: str
    observed_at: datetime
    runtime_version: str | None = None
    verifier_version: str | None = None
    capability_ref: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    tenant_id: str | None = None

    @property
    def counted(self) -> bool:
        return self.verdict != TrialVerdict.VERIFICATION_UNAVAILABLE

    @property
    def success(self) -> bool:
        return self.verdict == TrialVerdict.SUCCESS


class RevalTrial(_F):
    """The stored evidence row: a result plus its place in the case and its lineage verdict."""

    key: TrialKey
    result: TrialResult
    anchor: str
    accepted: bool = True
    reject_reason: RejectReason | None = None


class RevalTransition(_F):
    """One audited change of one case."""

    cell_key: str
    episode: int = Field(ge=0)
    cycle: int = Field(ge=0)
    seq: int = Field(ge=1)
    from_state: RevalState | None
    to_state: RevalState
    trigger: RevalTrigger
    reason: QuarantineReason
    strategy: RevalStrategy
    measurements: dict[str, float] = Field(default_factory=dict)
    detail: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    rebase: dict[str, str] = Field(default_factory=dict)
    n_trials: int = Field(default=0, ge=0)
    n_unverified: int = Field(default=0, ge=0)
    tokens_cycle: int = Field(default=0, ge=0)
    anchor: str
    observed_at: datetime | None = None
    params_hash: str
    applied: bool = True
    version: str = REVALIDATION_VERSION

    def signature(self) -> tuple[str, int, str | None, str, str, str]:
        return (
            self.cell_key,
            self.seq,
            self.from_state.value if self.from_state else None,
            self.to_state.value,
            self.trigger.value,
            self.anchor,
        )


def reval_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
