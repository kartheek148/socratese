from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OutcomeStatus(StrEnum):
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class TerminationReason(StrEnum):
    FINAL = "FINAL"
    ESCALATED = "ESCALATED"
    MAX_STEPS = "MAX_STEPS"
    MAX_TOOL_CALLS = "MAX_TOOL_CALLS"
    MAX_TOKENS = "MAX_TOKENS"
    DEADLINE = "DEADLINE"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    INVALID_ACTION = "INVALID_ACTION"
    LLM_ERROR = "LLM_ERROR"
    CAPABILITY_COMPLETED = "CAPABILITY_COMPLETED"
    CAPABILITY_ABORTED = "CAPABILITY_ABORTED"


class VerificationResult(BaseModel):
    """Evidence for one criterion. passed=None means the verifier could not evaluate it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_kind: str
    role: Literal["goal", "guard"]
    passed: bool | None
    detail: str = Field(max_length=500)
    evidence_ref: str | None = None


class RunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    task_id: str
    tenant_id: str
    config_id: str
    policy_version: str
    capability_version: str | None = None
    status: OutcomeStatus
    quality_score: float | None
    verification_results: list[VerificationResult]
    tokens_used: int
    tool_calls: int
    execution_time_ms: int
    human_interventions: int
    failure_reasons: list[str]
    evidence_refs: list[str]
    verifier_version: str


def aggregate_status(results: list[VerificationResult]) -> OutcomeStatus:
    """Derive the outcome only from verifier evidence.

    Any unevaluable criterion -> UNKNOWN. A failed guard -> FAILURE. Otherwise goals decide:
    all pass -> VERIFIED_SUCCESS, none -> FAILURE, some -> PARTIAL.
    """
    if not results or any(r.passed is None for r in results):
        return OutcomeStatus.UNKNOWN
    if any(r.role == "guard" and not r.passed for r in results):
        return OutcomeStatus.FAILURE
    goals = [r for r in results if r.role == "goal"]
    if not goals:
        return OutcomeStatus.UNKNOWN
    passed = sum(1 for r in goals if r.passed)
    if passed == len(goals):
        return OutcomeStatus.VERIFIED_SUCCESS
    return OutcomeStatus.PARTIAL if passed else OutcomeStatus.FAILURE


def quality_score(results: list[VerificationResult]) -> float | None:
    """Fraction of goal criteria passed; 0.0 if any guard failed; None if not evaluable."""
    if not results or any(r.passed is None for r in results):
        return None
    if any(r.role == "guard" and not r.passed for r in results):
        return 0.0
    goals = [r for r in results if r.role == "goal"]
    return sum(1 for r in goals if r.passed) / len(goals) if goals else None
