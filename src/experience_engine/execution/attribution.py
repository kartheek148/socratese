"""Build `StrategyExecutionResult` from a finished run (pure; never re-runs anything).

The independent verifier's final status is the only source of final success. The capability's own
outcome comes from the verification taken at the handover (when a planner follows) or from the
final verification (when no planner ran). Nothing is inferred across the phases.
"""

from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.strategy import (
    CapabilityPhase,
    FallbackPhase,
    StrategyExecutionResult,
)
from experience_engine.domain.task import TaskSpec
from experience_engine.execution.orchestrator import RunResult


def verified_of(status: str | None) -> bool | None:
    """VERIFIED_SUCCESS -> True; UNKNOWN or missing -> None (never a guess); anything else False."""
    if status is None or status == OutcomeStatus.UNKNOWN.value:
        return None
    return status == OutcomeStatus.VERIFIED_SUCCESS.value


def _common(result: RunResult, task: TaskSpec) -> dict[str, object]:
    o = result.outcome
    return {
        "controller_policy": result.decision.policy_version,
        "final_status": o.status.value,
        "final_verified": verified_of(o.status.value),
        "final_termination": result.runtime.termination.value,
        "total_tokens": o.tokens_used,
        "total_tool_calls": o.tool_calls,
        "total_latency_ms": o.execution_time_ms,
        "budget_tokens": task.max_tokens,
    }


def capability_strategy(
    result: RunResult, task: TaskSpec, *, asset_family: str | None, applicable: bool
) -> StrategyExecutionResult:
    """Strategy record for a run of the capability-aware orchestrator."""
    rt = result.runtime
    ref = result.outcome.capability_version
    common = _common(result, task)
    final = str(common["final_status"])
    phase = rt.capability_phase
    if ref is None or phase is None:
        return StrategyExecutionResult(
            strategy_id="planning",
            applicable=applicable,
            attempted=False,
            **common,
        )
    fb = rt.fallback_phase or FallbackPhase()
    if fb.triggered:
        measured = rt.handover_status is not None
        cap = phase.model_copy(
            update={
                "outcome": rt.handover_status,
                "verified": verified_of(rt.handover_status),
                "outcome_measured": measured,
            }
        )
        fb = fb.model_copy(update={"outcome": final, "verified": verified_of(final)})
    else:
        cap = phase.model_copy(
            update={"outcome": final, "verified": verified_of(final), "outcome_measured": True}
        )
    return StrategyExecutionResult(
        strategy_id="direct-capability",
        asset_kind="CAPABILITY",
        asset_ref=ref,
        asset_family=asset_family,
        applicable=True,
        attempted=True,
        capability=cap,
        fallback=fb,
        **common,
    )


def memory_strategy(
    result: RunResult,
    task: TaskSpec,
    *,
    asset_ref: str | None,
    asset_family: str | None,
) -> StrategyExecutionResult:
    """Memory-assisted (or plain) general-agent run: a single phase, no fallback concept."""
    common = _common(result, task)
    used = asset_ref is not None
    return StrategyExecutionResult(
        strategy_id="memory-assisted" if used else "planning",
        asset_kind="MEMORY" if used else None,
        asset_ref=asset_ref,
        asset_family=asset_family,
        applicable=used,
        attempted=used,
        **common,
    )


def capability_view(s: StrategyExecutionResult) -> CapabilityPhase | None:
    return s.capability
