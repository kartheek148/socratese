"""Evaluation and promotion contracts (milestone 4). Pure data + hashing; the decision logic lives
in `evaluation/policy.py` and the lifecycle in `capabilities/registry.py`.
"""

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class LifecycleCost(_Frozen):
    """Cost of owning the capability, in the same units as the baseline's per-task cost."""

    learning_tokens: int
    learning_wall_ms: int
    evaluation_overhead_tokens: int  # tokens spent on control arms A/B to evaluate C
    capability_exec_tokens: int
    capability_verified: int
    lifecycle_tokens_per_success: float | None
    baseline_tokens_per_success: float | None


class EvaluationReport(_Frozen):
    capability_id: str
    version: int
    tenant_id: str
    risk_class: Literal["low", "medium", "high"]
    policy_name: str
    policy_hash: str
    policy: PromotionPolicy
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

    def n_in(self, suite: Suite) -> int:
        arms = self.suites.get(suite.value, {})
        m = arms.get(Arm.CAPABILITY.value)
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
