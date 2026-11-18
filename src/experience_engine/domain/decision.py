from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.outcome import OutcomeStatus


class Strategy(StrEnum):
    DIRECT_CAPABILITY = "DIRECT_CAPABILITY"
    COMPOSE_CAPABILITIES = "COMPOSE_CAPABILITIES"
    GENERAL_PLANNING = "GENERAL_PLANNING"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"


class ControllerDecision(BaseModel):
    """Engineering rule 10: every decision records the eligible set, choice, policy and outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    run_id: str
    tenant_id: str
    policy_version: str
    feature_snapshot: dict[str, object]
    eligible_actions: list[Strategy] = Field(min_length=1)
    estimates: dict[str, float] = Field(default_factory=dict)
    selected_action: Strategy
    selection_probability: float | None
    reason: str
    outcome_status: OutcomeStatus | None = None
