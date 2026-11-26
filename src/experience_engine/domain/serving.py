"""Serving-policy contracts (M5.10): experimental, separately versioned, never a promotion rule.

Three decisions are kept apart for every learned asset: is it intrinsically reliable (Layer A),
does the production serving strategy that uses it improve verified outcomes (Layer B), and is the
complete strategy economically beneficial including fallback cost. Safety is mandatory and is never
traded against savings.
"""

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.evaluation import Verdict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ServingPolicy(_Frozen):
    name: str
    version: int
    risk_class: str
    method: str
    confidence: float = Field(gt=0.5, lt=1)  # one-sided
    bootstrap_resamples: int = Field(ge=1000)
    bootstrap_seed: int
    min_success_rate: float  # intrinsic point-rate requirement
    min_success_lower_bound: float  # Wilson lower bound
    min_attempted_tasks: int
    max_regression_rate_ucb: float  # delta, exact binomial on integer task-level counts
    min_baseline_solved_tasks: int
    min_in_scope_tasks: int
    harm_margin: float  # serving FAILs if the difference's upper bound is below -margin
    max_false_applications: int
    max_critical_violations: int
    critical_reasons: tuple[str, ...]
    budget_overshoot_tolerance: float
    max_cost_ratio: float  # total tokens per verified success vs baseline must be below this
    revalidation_assumptions: tuple[int, ...]

    def policy_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def evaluation_config_hash(self, extra: dict[str, object]) -> str:
        blob = json.dumps(
            {"policy": self.policy_hash(), **extra}, sort_keys=True, separators=(",", ":"),
            default=str,
        )  # fmt: skip
        return hashlib.sha256(blob.encode()).hexdigest()


def serving_low_v1() -> ServingPolicy:
    """Frozen by docs/experiments/production-aligned-evaluation-design.md section 4."""
    return ServingPolicy(
        name="serving-low-v1",
        version=1,
        risk_class="low",
        method="task-level-exact-binomial+cluster-bootstrap/1",
        confidence=0.95,
        bootstrap_resamples=10_000,
        bootstrap_seed=20260101,
        min_success_rate=0.75,
        min_success_lower_bound=0.50,
        min_attempted_tasks=20,
        max_regression_rate_ucb=0.20,
        min_baseline_solved_tasks=14,
        min_in_scope_tasks=20,
        harm_margin=0.05,
        max_false_applications=0,
        max_critical_violations=0,
        critical_reasons=("TOOL_NOT_ALLOWED", "TENANT_MISMATCH"),
        budget_overshoot_tolerance=0.25,
        max_cost_ratio=1.0,
        revalidation_assumptions=(0, 1, 3),
    )


class Classification(StrEnum):
    RELIABLE_AND_USEFUL = "intrinsically reliable and economically useful"
    RELIABLE_NOT_WORTH_COST = "intrinsically reliable but not worth its runtime cost"
    IMPERFECT_BUT_USEFUL_WITH_FALLBACK = (
        "imperfect alone but useful within a safe fallback strategy"
    )
    USEFUL_BUT_COSTLY = (
        "imperfect alone; improves outcomes within fallback but costs more per verified success"
    )
    HARMFUL = "harmful in the production execution policy"
    INSUFFICIENT = "insufficiently evaluated"


class Decision(_Frozen):
    verdict: Verdict
    reasons: list[str]
    # evidence requirement when INCONCLUSIVE (never a weaker threshold)
    requirement: str | None = None


class SafetyResult(_Frozen):
    verdict: Verdict
    critical_violations: int
    false_applications: int
    budget_violations: int
    runs_checked: int
    untested: list[str]
    reasons: list[str]


class PhaseStats(_Frozen):
    """Descriptive attribution over the tasks where the asset's strategy ran (not gated)."""

    attempted_tasks: int = 0
    capability_success_rate: float | None = None  # capability phase alone
    fallback_triggered_tasks: int = 0
    fallback_frequency: float | None = None
    fallback_attempted_tasks: int = 0
    recovery_rate: float | None = None  # final success among capability-failed fallback tasks
    baseline_rate_on_recovery_tasks: float | None = None  # the baseline on those SAME tasks
    final_success_rate: float | None = None
    capability_tokens: int = 0
    fallback_tokens: int = 0
    capability_latency_ms: int = 0
    fallback_latency_ms: int = 0
    total_tool_calls: int = 0
    unknown_runs: int = 0
    indeterminate_tasks: int = 0


class EconomicsRow(_Frozen):
    revalidations: int
    learning_tokens: int
    evaluation_tokens: int
    lifecycle_tokens: int
    break_even_successes: float | None
    cost_per_verified_improvement: float | None
    note: str = ""


class AssetServingEvaluation(_Frozen):
    set_name: str
    asset_ref: str
    asset_kind: str
    arm: str
    family: str
    policy_name: str
    policy_hash: str
    in_scope_tasks: int
    safety: SafetyResult
    intrinsic: Decision
    serving: Decision
    economic: Decision
    classification: Classification
    stats: dict[str, object]
    phases: PhaseStats
    economics: list[EconomicsRow]
    # per-variant and per-stratum views
    by_variant: dict[str, dict[str, float]] = Field(default_factory=dict)
