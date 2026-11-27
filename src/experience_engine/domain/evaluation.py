"""Evaluation and promotion contracts (milestone 4). Pure data + hashing; the decision logic lives
in `evaluation/policy.py` and the lifecycle in `capabilities/registry.py`.
"""

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from experience_engine.domain.time import UtcDatetime


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Suite(StrEnum):
    SOURCE_REPLAY = "SOURCE_REPLAY"  # necessary, never sufficient
    HELD_OUT = "HELD_OUT"
    TRANSFER = "TRANSFER"
    NEGATIVE = "NEGATIVE"
    REGRESSION = "REGRESSION"


class Arm(StrEnum):
    BASELINE = "A_BASELINE"  # fixed baseline agent, no persistent learning
    MEMORY = "B_MEMORY"  # baseline + episodic hint only
    CAPABILITY = "C_CAPABILITY"  # compiled capability, fixed strategy
    MEMORY_ASSET = "M_MEMORY_ASSET"  # baseline agent + one governed memory artifact (M5.8)
    SERVING = "E_SERVING"  # M6.2: the capability as production serves it (planner fallback)


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class PromotionPolicy(_Frozen):
    """Explicit, versioned promotion criteria for one risk class. Hard constraints are gates,
    not terms in a reward: all must hold, and none can be traded off against token savings.
    """

    name: str
    version: int = 1
    risk_class: Literal["low", "medium", "high"]
    # evidence required before any verdict other than INCONCLUSIVE
    min_heldout_n: int = Field(ge=1)
    min_transfer_n: int = Field(ge=1)
    min_negative_n: int = Field(ge=1)
    # correctness gates (rates are fractions of tasks in the suite)
    min_source_replay_success_rate: float = Field(ge=0, le=1)
    min_heldout_success_rate: float = Field(ge=0, le=1)
    min_transfer_success_rate: float = Field(ge=0, le=1)
    min_success_lower_bound: float = Field(ge=0, le=1)  # Wilson 95% lower bound, held-out+transfer
    max_false_applicability_rate: float = Field(ge=0, le=1)
    max_regressions: int = Field(ge=0)
    # cost gate: capability execution tokens per verified success vs baseline (None = not gated)
    max_exec_token_ratio: float | None = Field(default=None, gt=0)
    # rollout gates
    min_shadow_observations: int = Field(ge=1)
    max_shadow_failure_rate: float = Field(ge=0, le=1)
    min_canary_observations: int = Field(ge=1)
    min_canary_success_rate: float = Field(ge=0, le=1)
    canary_fraction: float = Field(gt=0, le=1)

    def policy_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class ArmMetrics(_Frozen):
    n: int
    verified: int
    rate: float
    ci_low: float
    ci_high: float
    tokens_total: int
    tool_calls_total: int
    latency_mean_ms: float
    tokens_per_success: float | None
    tool_calls_per_success: float | None
    # capability arm only
    applicable: int | None = None
    fell_back: int | None = None
    # distinct tasks behind `n` runs (repeats of one task are correlated, not independent)
    n_tasks: int | None = None


class TaskResult(_Frozen):
    suite: Suite
    arm: Arm
    task_id: str
    family: str
    variant: str
    status: str  # OutcomeStatus value, or NOT_APPLICABLE for a correct refusal
    tokens: int
    tool_calls: int
    latency_ms: int
    applicable: bool | None = None
    termination: str | None = None
    seed: int = 0  # repetition identifier: the same task is re-run once per seed
    cache_hit: bool = False  # served from a previously computed run (not executed now)
    prompt_tokens: int | None = None  # split known only for general-planning model calls
    completion_tokens: int | None = None
    # M6.2 serving arm only (omitted from the serialised form when unset)
    capability_used: bool | None = None
    fell_back: bool | None = None
    asset_ref: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_serving(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("capability_used", "fell_back", "asset_ref"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ServingEvidence(_Frozen):
    """Final-outcome paired evidence recorded by the evaluation workflow (M6.2-A).

    The unit is the production outcome: the capability served with its planner fallback (default)
    against the stateless planner (alternative), per task over HELD_OUT and TRANSFER repetitions
    (strict majority; UNKNOWN and ties dropped and counted). It never enters a promotion decision.
    """

    evidence_kind: Literal["PROMOTION_EVALUATION"] = "PROMOTION_EVALUATION"
    capability_ref: str  # id@version
    family: str
    risk_class: Literal["low", "medium", "high"]
    suites: list[str]
    n: int
    both: int
    default_only: int
    alt_only: int
    neither: int
    default_tokens: int  # capability + fallback, summed over the paired tasks
    alt_tokens: int  # planner
    n_unknown_dropped: int
    n_tied_dropped: int
    n_not_offered: int  # tasks where no capability was used by the serving arm
    n_missing_cost: int
    n_duplicate_rows: int
    tasks_sha256: str
    capability_hash: str = ""
    catalog: str = "EVALUATION_STATIC_CATALOG"  # never an ACTIVE production catalog
    serving_tokens: int = 0  # all tokens the serving arm spent in this evaluation
    n_runs: int = 0


class LifecycleCost(_Frozen):
    """Cost of owning the capability, in the same units as the baseline's per-task cost."""

    learning_tokens: int
    learning_wall_ms: int
    evaluation_overhead_tokens: int  # tokens spent on control arms A/B to evaluate C
    capability_exec_tokens: int
    capability_verified: int
    lifecycle_tokens_per_success: float | None
    baseline_tokens_per_success: float | None


class RiskBasedPolicy(PromotionPolicy):
    """Risk-based criteria (M5.9, `*-v2`). Extends the count-based policy with task-level bounds.
    The extra fields are *required*: a v1 JSON can never validate as v2 and vice versa, and the
    v1 `policy_hash()` is unaffected because these fields exist only here. `max_regressions` is
    kept for schema compatibility and is not read by the risk-based verdict."""

    method: Literal["task-cluster-clopper-pearson/1"]
    confidence: float = Field(gt=0.5, lt=1)  # one-sided
    max_regression_rate_ucb: float = Field(ge=0, le=1)  # delta
    min_regression_tasks: int = Field(ge=1)  # distinct baseline-solved tasks
    noninferiority_margin: float = Field(ge=0, le=1)
    max_variant_regression_rate: float = Field(ge=0, le=1)
    min_variant_pairs: int = Field(ge=1)
    max_critical_violations: int = Field(ge=0)
    critical_terminations: tuple[str, ...]
    bootstrap_resamples: int = Field(ge=1000)
    bootstrap_seed: int


AnyPolicy = RiskBasedPolicy | PromotionPolicy


class RiskEvidence(_Frozen):
    """Task-level evidence a risk-based policy needs, computed from the per-task rows."""

    n_pairs: int  # (suite, task, repetition) pairs where the baseline verified
    n_regressions: int  # of those, pairs the treatment failed
    n_dropped_unknown: int  # pairs dropped because either side was UNKNOWN (missing data)
    regression_tasks: int  # distinct tasks with at least one baseline-solved repetition
    regression_mass: float  # sum over those tasks of regressed / baseline-solved repetitions
    improvement_tasks: int  # tasks where the treatment verified more often than the baseline
    variant_pairs: dict[str, list[int]]  # variant -> [baseline-solved pairs, regressions]
    paired_diffs: list[float]  # per task (held-out + transfer): treatment rate - baseline rate
    source_tasks: int
    source_mass: float  # sum over source tasks of the treatment success fraction
    termination_counts: dict[str, int]  # treatment terminations over all runs


class CounterfactualVerdict(_Frozen):
    """What another policy would have decided on the same evidence. Never acted on."""

    policy_name: str
    policy_hash: str
    verdict: Verdict
    reasons: list[str]


class ArmResources(_Frozen):
    runs_executed: int = 0
    runs_cached: int = 0
    tokens_executed: int = 0
    tokens_cached: int = 0  # as recorded when the run was first computed
    prompt_tokens: int = 0  # over executed runs whose split is known
    completion_tokens: int = 0
    runs_with_split: int = 0
    tool_calls_executed: int = 0
    latency_ms_executed: int = 0


class ValidationResources(_Frozen):
    """Measured cost of one evaluation, from its per-task rows (never extrapolated)."""

    by_arm: dict[str, ArmResources]
    early_stopped: bool = False
    stage_reached: int | None = None


class EvaluationReport(_Frozen):
    capability_id: str  # the asset id: a capability id or, for memory, a memory id
    asset_kind: str = "CAPABILITY"
    treatment_arm: str = Arm.CAPABILITY.value  # the arm whose metrics are gated
    version: int
    tenant_id: str
    risk_class: Literal["low", "medium", "high"]
    policy_name: str
    policy_hash: str
    policy: AnyPolicy
    verdict: Verdict
    reasons: list[str]
    suites: dict[str, dict[str, ArmMetrics]]  # suite -> arm -> metrics
    false_applicability_rate: float | None
    regressions: int
    success_lower_bound: float | None
    lifecycle: LifecycleCost
    dataset_versions: dict[str, dict[str, str]]  # split -> {name, version, content_hash}
    model_settings: dict[str, object]
    seeds: list[int]
    runtime_version: str
    verifier_version: str
    environment_version: str
    evaluator_version: str
    started_at: UtcDatetime
    completed_at: UtcDatetime
    task_results: list[TaskResult] = Field(default_factory=list)
    # M5.9 (all optional so every earlier report still loads)
    risk_evidence: RiskEvidence | None = None
    counterfactuals: list[CounterfactualVerdict] = Field(default_factory=list)
    evaluation_config_hash: str | None = None
    stage_reached: int | None = None
    early_stopped: bool = False
    resources: ValidationResources | None = None
    # M6.2 (optional; omitted from the serialised form when None, so earlier reports are unchanged)
    serving_evidence: ServingEvidence | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_serving_evidence(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.serving_evidence is None:
            data.pop("serving_evidence", None)
        return data

    def n_in(self, suite: Suite) -> int:
        arms = self.suites.get(suite.value, {})
        m = arms.get(self.treatment_arm)
        return m.n if m else 0


def default_policies() -> dict[str, PromotionPolicy]:
    """Built-in criteria per risk class. Stricter classes need more evidence and tighter gates;
    `high` needs more held-out/transfer tasks than the bundled benchmark provides, so it stays
    INCONCLUSIVE there by design. Override with a reviewed policy file (see evaluation/policy.py).
    """
    return {
        "low": PromotionPolicy(
            name="default-low",
            risk_class="low",
            min_heldout_n=4,
            min_transfer_n=4,
            min_negative_n=3,
            min_source_replay_success_rate=0.8,
            min_heldout_success_rate=0.75,
            min_transfer_success_rate=0.75,
            min_success_lower_bound=0.5,
            max_false_applicability_rate=0.0,
            max_regressions=1,
            max_exec_token_ratio=None,
            min_shadow_observations=5,
            max_shadow_failure_rate=0.2,
            min_canary_observations=5,
            min_canary_success_rate=0.7,
            canary_fraction=0.2,
        ),
        "medium": PromotionPolicy(
            name="default-medium",
            risk_class="medium",
            min_heldout_n=4,
            min_transfer_n=4,
            min_negative_n=3,
            min_source_replay_success_rate=0.9,
            min_heldout_success_rate=0.85,
            min_transfer_success_rate=0.85,
            min_success_lower_bound=0.6,
            max_false_applicability_rate=0.0,
            max_regressions=0,
            max_exec_token_ratio=1.0,
            min_shadow_observations=10,
            max_shadow_failure_rate=0.1,
            min_canary_observations=10,
            min_canary_success_rate=0.85,
            canary_fraction=0.1,
        ),
        "high": PromotionPolicy(
            name="default-high",
            risk_class="high",
            min_heldout_n=8,
            min_transfer_n=8,
            min_negative_n=5,
            min_source_replay_success_rate=0.95,
            min_heldout_success_rate=0.95,
            min_transfer_success_rate=0.95,
            min_success_lower_bound=0.8,
            max_false_applicability_rate=0.0,
            max_regressions=0,
            max_exec_token_ratio=1.0,
            min_shadow_observations=20,
            max_shadow_failure_rate=0.05,
            min_canary_observations=20,
            min_canary_success_rate=0.95,
            canary_fraction=0.05,
        ),
    }


def default_policies_v2() -> dict[str, RiskBasedPolicy]:
    """Candidate risk-based criteria (`*-v2`), frozen by the M5.9 design document.
    Success, lower-bound, false-applicability, sample-size, token-ratio and rollout fields equal
    the v1 values of the same risk class; only the regression and source-replay tests differ."""
    v1 = default_policies()
    common = {
        "method": "task-cluster-clopper-pearson/1",
        "max_critical_violations": 0,
        "critical_terminations": ("TOOL_NOT_ALLOWED",),
        "bootstrap_resamples": 10_000,
        "bootstrap_seed": 20260101,
        "min_variant_pairs": 6,
        "max_regressions": 1_000_000,  # superseded by max_regression_rate_ucb
    }
    spec = {
        "low": dict(confidence=0.95, max_regression_rate_ucb=0.20, min_regression_tasks=8,
                    noninferiority_margin=0.05, max_variant_regression_rate=0.5),
        "medium": dict(confidence=0.95, max_regression_rate_ucb=0.10, min_regression_tasks=15,
                       noninferiority_margin=0.02, max_variant_regression_rate=0.3),
        "high": dict(confidence=0.975, max_regression_rate_ucb=0.05, min_regression_tasks=30,
                     noninferiority_margin=0.0, max_variant_regression_rate=0.15),
    }  # fmt: skip
    out: dict[str, RiskBasedPolicy] = {}
    for risk, base in v1.items():
        data = {**base.model_dump(), **common, **spec[risk]}
        data["name"] = f"{base.name}-v2"
        data["version"] = 2
        out[risk] = RiskBasedPolicy.model_validate(data)
    return out
