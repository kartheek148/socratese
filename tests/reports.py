"""Builders for evaluation reports used across registry/evaluation tests."""

from datetime import UTC, datetime

from experience_engine.domain.evaluation import (
    Arm,
    ArmMetrics,
    EvaluationReport,
    LifecycleCost,
    PromotionPolicy,
    Suite,
    Verdict,
)
from experience_engine.domain.stats import wilson_interval


def arm_metrics(n: int, ok: int, tokens: int = 1000) -> ArmMetrics:
    lo, hi = wilson_interval(ok, n)
    return ArmMetrics(
        n=n,
        verified=ok,
        rate=ok / n if n else 0.0,
        ci_low=lo,
        ci_high=hi,
        tokens_total=tokens,
        tool_calls_total=n,
        latency_mean_ms=1.0,
        tokens_per_success=tokens / ok if ok else None,
        tool_calls_per_success=1.0 if ok else None,
    )


def make_report(
    *,
    capability_id: str,
    version: int,
    tenant: str,
    verdict: Verdict,
    policy: PromotionPolicy,
    transfer_n: int = 4,
) -> EvaluationReport:
    c = Arm.CAPABILITY.value
    return EvaluationReport(
        capability_id=capability_id,
        version=version,
        tenant_id=tenant,
        risk_class=policy.risk_class,
        policy_name=policy.name,
        policy_hash=policy.policy_hash(),
        policy=policy,
        verdict=verdict,
        reasons=["synthetic"],
        suites={
            Suite.SOURCE_REPLAY.value: {c: arm_metrics(6, 6)},
            Suite.HELD_OUT.value: {c: arm_metrics(4, 4)},
            Suite.TRANSFER.value: {c: arm_metrics(transfer_n, transfer_n)},
            Suite.NEGATIVE.value: {c: arm_metrics(6, 6)},
        },
        false_applicability_rate=0.0,
        regressions=0,
        success_lower_bound=0.7,
        lifecycle=LifecycleCost(
            learning_tokens=0,
            learning_wall_ms=1,
            evaluation_overhead_tokens=2,
            capability_exec_tokens=3,
            capability_verified=4,
            lifecycle_tokens_per_success=1.0,
            baseline_tokens_per_success=2.0,
        ),
        dataset_versions={},
        model_settings={},
        seeds=[0],
        runtime_version="r",
        verifier_version="v",
        environment_version="e",
        evaluator_version="x",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
