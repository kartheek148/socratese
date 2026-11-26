"""Execution-level attribution for learned-asset strategies (pure data, M5.10).

A production run that uses a compiled capability can end in the general planner. These records keep
the capability phase and the fallback phase apart so that nothing is inferred across them:
capability success is never inferred from the final outcome, final failure is never inferred from
a capability failure, and UNKNOWN stays UNKNOWN (`None`).
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FallbackCause(StrEnum):
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    TIMEOUT_OR_BUDGET = "TIMEOUT_OR_BUDGET"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    DECISION_FAILURE = "DECISION_FAILURE"
    INAPPLICABLE_AT_EXECUTION = "INAPPLICABLE_AT_EXECUTION"


_CAUSE_BY_REASON: dict[str, FallbackCause] = {
    "TOOL_FAILED": FallbackCause.TOOL_FAILURE,
    "TIMEOUT": FallbackCause.TIMEOUT_OR_BUDGET,
    "MAX_STEPS": FallbackCause.TIMEOUT_OR_BUDGET,
    "MAX_TOKENS": FallbackCause.TIMEOUT_OR_BUDGET,
    "VERIFY_FAILED": FallbackCause.VERIFICATION_FAILURE,
    "VERIFY_UNKNOWN": FallbackCause.VERIFICATION_FAILURE,
    "DECISION_FAILED": FallbackCause.DECISION_FAILURE,
    "PRECONDITION_VIOLATED": FallbackCause.INAPPLICABLE_AT_EXECUTION,
}
# reasons that stop the run without a planner (see capabilities/executor.py)
CRITICAL_REASONS = frozenset({"TOOL_NOT_ALLOWED", "TENANT_MISMATCH"})


def fallback_cause(reason: str) -> FallbackCause:
    """Map an executor reason code to the cause category (everything else is a plain failure)."""
    return _CAUSE_BY_REASON.get(reason, FallbackCause.EXECUTION_FAILURE)


class CapabilityPhase(_Frozen):
    """What the capability did, judged on the environment as it left it."""

    status: str  # ExecStatus: COMPLETED | FALLBACK | ABORTED | ESCALATE | NOT_APPLICABLE
    reason: str  # ExecReason
    detail: str = ""
    outcome: str | None = None  # independent verifier status at the end of the phase
    verified: bool | None = None  # None = UNKNOWN or not measured, never a guess
    tokens: int = 0
    tool_calls: int = 0
    steps: int = 0
    latency_ms: int = 0
    # a handover verification was not taken (attribution disabled): `verified` is then None
    outcome_measured: bool = True


class FallbackPhase(_Frozen):
    triggered: bool = False  # the capability handed over to the planner
    attempted: bool = False  # the planner actually started (budget may already be spent)
    cause: FallbackCause | None = None
    reason: str | None = None
    remaining_tokens: int | None = None
    remaining_tool_calls: int | None = None
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    latency_ms: int = 0
    outcome: str | None = None  # independent verifier status of the FINAL state
    verified: bool | None = None


class StrategyExecutionResult(_Frozen):
    """One production execution of a strategy (capability, memory-assisted or plain planning)."""

    strategy_id: Literal["direct-capability", "memory-assisted", "planning"]
    asset_kind: Literal["CAPABILITY", "MEMORY"] | None = None
    asset_ref: str | None = None  # exact id@version
    asset_family: str | None = None
    controller_policy: str | None = None
    applicable: bool = False
    attempted: bool = False
    capability: CapabilityPhase | None = None
    fallback: FallbackPhase = Field(default_factory=FallbackPhase)
    final_status: str  # OutcomeStatus value
    final_verified: bool | None  # None = UNKNOWN
    final_termination: str | None = None
    total_tokens: int = 0
    total_tool_calls: int = 0
    total_latency_ms: int = 0
    budget_tokens: int = 0

    @property
    def capability_verified(self) -> bool | None:
        return self.capability.verified if self.capability else None

    @property
    def tokens_by_phase(self) -> tuple[int, int]:
        cap = self.capability.tokens if self.capability else 0
        return cap, self.fallback.tokens

    def phases_consistent(self) -> bool:
        """Totals are the sum of the phases (memory/planning runs have a single phase)."""
        if self.capability is None:
            return self.total_tokens >= 0
        return self.total_tokens == self.capability.tokens + self.fallback.tokens
