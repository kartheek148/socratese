from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.capability import PathRole
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.time import UtcDatetime

PIPELINE_VERSION = "experience/1"
NORMALIZER_VERSION = "normalizer/1"
DISCOVERY_SPLIT = "DISCOVERY"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NormalizedStep(_Frozen):
    """One abstracted step. `token` is the variant-independent identity the miner compares:
    `<tool>` or `<tool>:<path role>`; raw paths/values are deliberately not part of it.
    """

    index: int
    kind: Literal["llm", "tool"]
    token: str
    tool: str | None = None
    role: PathRole | None = None
    ok: bool | None = None
    value_type: Literal["string", "number", "boolean"] | None = None
    tokens: int = 0


class Trajectory(_Frozen):
    run_id: str
    tenant_id: str
    task_id: str
    task_family: str | None
    outcome_status: OutcomeStatus
    steps: list[NormalizedStep]
    tokens_used: int
    tool_calls: int
    claimed_done: bool
    termination: str
    # environment features observed during the run
    risk_level: str
    allowed_tools: list[str]
    file_roles: list[str]
    config_shape: str | None
    normalizer_version: str = NORMALIZER_VERSION

    @property
    def tool_tokens(self) -> list[str]:
        return [s.token for s in self.steps if s.kind == "tool"]


class ExclusionReason(StrEnum):
    NON_DISCOVERY_SPLIT = "NON_DISCOVERY_SPLIT"
    UNKNOWN_PROVENANCE = "UNKNOWN_PROVENANCE"
    UNVERIFIED_OUTCOME = "UNVERIFIED_OUTCOME"
    INCOMPLETE_RUN = "INCOMPLETE_RUN"


class ExperienceCase(_Frozen):
    """Derived view of one run for learning. The raw trace remains the source of truth."""

    case_id: str
    tenant_id: str
    run_id: str
    task_id: str
    pipeline_version: str = PIPELINE_VERSION
    task_family: str | None
    outcome_status: OutcomeStatus
    dataset_split: str | None
    eligible: bool
    excluded_reason: ExclusionReason | None = None
    features: dict[str, object]
    trajectory_sha256: str | None = None
    n_steps: int = 0
    tokens_used: int = 0
    created_at: UtcDatetime


def classify_eligibility(
    outcome: OutcomeStatus, dataset_split: str | None, run_completed: bool
) -> tuple[bool, ExclusionReason | None]:
    """Rule 9: learn only from completed, verified-outcome runs of known provenance, and only
    from the discovery split (validation / hidden-transfer data must never reach the compiler).
    """
    if not run_completed:
        return False, ExclusionReason.INCOMPLETE_RUN
    if dataset_split is None:
        return False, ExclusionReason.UNKNOWN_PROVENANCE
    if dataset_split != DISCOVERY_SPLIT:
        return False, ExclusionReason.NON_DISCOVERY_SPLIT
    if outcome not in (OutcomeStatus.VERIFIED_SUCCESS, OutcomeStatus.FAILURE):
        return False, ExclusionReason.UNVERIFIED_OUTCOME
    return True, None


class FindingKind(StrEnum):
    CRITICAL = "CRITICAL"  # characteristic of successes, rare in failures
    AVOIDABLE = "AVOIDABLE"  # characteristic of failures, rare in successes
    NEUTRAL = "NEUTRAL"


class ContrastiveFinding(_Frozen):
    """A hypothesis about association, not a causal claim."""

    token: str
    kind: FindingKind
    success_support: float = Field(ge=0, le=1)
    failure_support: float = Field(ge=0, le=1)
    n_success: int
    n_failure: int
