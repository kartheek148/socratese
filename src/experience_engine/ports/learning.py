from enum import StrEnum
from typing import Protocol

from experience_engine.domain.run import ExecutionCompletedPayload, LearningJob


class EnqueueResult(StrEnum):
    ENQUEUED = "ENQUEUED"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"  # this consumer already processed this event_id
    DUPLICATE_JOB = "DUPLICATE_JOB"  # a job with the same dedupe key already exists


class LearningStore(Protocol):
    def register_and_enqueue(
        self, consumer: str, event: ExecutionCompletedPayload, job: LearningJob
    ) -> EnqueueResult:
        """One transaction: record the delivery (inbox) and insert the job if its key is new."""
        ...

    def record_dead_letter(
        self,
        *,
        consumer: str,
        event_id: str | None,
        tenant_id: str | None,
        reason: str,
        raw: str,
        attempts: int,
    ) -> None: ...
