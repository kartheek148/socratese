import uuid
from collections.abc import Mapping

from pydantic import ValidationError

from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.run import (
    PIPELINE_VERSION,
    TRAJECTORY_JOB,
    ExecutionCompletedPayload,
    LearningJob,
)
from experience_engine.ports.clock import Clock
from experience_engine.ports.learning import EnqueueResult, LearningStore

CONSUMER = "learning.collector"


class NonRetryableEventError(Exception):
    """The message can never succeed (malformed, unknown schema): dead-letter it immediately."""


class ExecutionCompletedHandler:
    """Idempotent consumer of `execution.completed`.

    Delivery is at-least-once, so the same event may arrive repeatedly. The inbox row (consumer,
    event_id) and the job's dedupe key (run_id, pipeline_version) are written in one transaction
    by the store, so redelivery never creates a second job and a crash never loses one.

    Every outcome is enqueued with its true status; only VERIFIED_SUCCESS runs are flagged as
    eligible for success mining (UNKNOWN/PARTIAL/FAILURE are never learned from as successes).
    """

    def __init__(self, store: LearningStore, clock: Clock):
        self._store = store
        self._clock = clock

    def parse(self, raw: Mapping[str, object]) -> ExecutionCompletedPayload:
        try:
            return ExecutionCompletedPayload.model_validate(raw)
        except ValidationError as exc:
            raise NonRetryableEventError(
                f"invalid execution.completed payload: {exc.error_count()} error(s)"
            ) from exc

    def handle(self, raw: Mapping[str, object]) -> EnqueueResult:
        event = self.parse(raw)
        job = LearningJob(
            job_id=new_id(),
            tenant_id=event.tenant_id,
            job_type=TRAJECTORY_JOB,
            dedupe_key=f"{event.run_id}:{PIPELINE_VERSION}",
            run_id=event.run_id,
            outcome_status=event.outcome_status,
            eligible_for_success_mining=event.outcome_status == OutcomeStatus.VERIFIED_SUCCESS,
            correlation_id=event.correlation_id,
            created_at=self._clock.now(),
        )
        return self._store.register_and_enqueue(CONSUMER, event, job)

    def dead_letter(
        self, raw: Mapping[str, object], reason: str, attempts: int, raw_text: str
    ) -> None:
        # Poison payloads may carry garbage ids; the raw text is preserved either way.
        event_id = _uuid_or_none(raw.get("event_id"))
        tenant_id = raw.get("tenant_id")
        self._store.record_dead_letter(
            consumer=CONSUMER,
            event_id=event_id,
            tenant_id=tenant_id[:200] if isinstance(tenant_id, str) else None,
            reason=reason,
            raw=raw_text,
            attempts=attempts,
        )


def _uuid_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None
