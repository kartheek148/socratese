from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.time import UtcDatetime


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    tenant_id: str
    task_id: str
    correlation_id: str
    config_id: str
    policy_version: str
    runtime_version: str
    analyzer_version: str | None = None
    model_settings: dict[str, object] = Field(default_factory=dict)
    status: RunStatus = RunStatus.RUNNING
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    DEAD_LETTERED = "DEAD_LETTERED"


EXECUTION_COMPLETED = "execution.completed"
PAYLOAD_SCHEMA_VERSION = 1


class ExecutionCompletedPayload(BaseModel):
    """Body of the `execution.completed` event. Small and self-describing: consumers load the
    trace and artifacts by reference instead of receiving them in the message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = PAYLOAD_SCHEMA_VERSION
    event_id: str
    tenant_id: str
    run_id: str
    task_id: str
    correlation_id: str
    outcome_status: OutcomeStatus
    config_id: str
    policy_version: str
    verifier_version: str
    occurred_at: UtcDatetime


class OutboxEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    tenant_id: str
    type: str
    aggregate_id: str
    payload: dict[str, object]
    correlation_id: str
    created_at: UtcDatetime
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0


class LearningJobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


TRAJECTORY_JOB = "trajectory_build"
PIPELINE_VERSION = "m2-enqueue/1"


class LearningJob(BaseModel):
    """Queued learning work. M2 only enqueues; trajectory building arrives in milestone 3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    tenant_id: str
    job_type: str = TRAJECTORY_JOB
    dedupe_key: str
    run_id: str
    outcome_status: OutcomeStatus
    # Only verified successes may be mined as successes (engineering rule 9).
    eligible_for_success_mining: bool
    pipeline_version: str = PIPELINE_VERSION
    correlation_id: str
    created_at: UtcDatetime
