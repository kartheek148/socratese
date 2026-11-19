from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from experience_engine.adapters.db import tables as t
from experience_engine.domain.ids import new_id
from experience_engine.domain.run import ExecutionCompletedPayload, LearningJob, LearningJobState
from experience_engine.ports.learning import EnqueueResult


class SqlLearningStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def register_and_enqueue(
        self, consumer: str, event: ExecutionCompletedPayload, job: LearningJob
    ) -> EnqueueResult:
        with self._engine.begin() as conn:
            inserted = conn.execute(
                pg_insert(t.consumed_events)
                .values(consumer=consumer, event_id=event.event_id, consumed_at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=["consumer", "event_id"])
            )
            if inserted.rowcount == 0:
                return EnqueueResult.DUPLICATE_DELIVERY
            created = conn.execute(
                pg_insert(t.learning_jobs)
                .values(
                    job_id=job.job_id,
                    tenant_id=job.tenant_id,
                    job_type=job.job_type,
                    dedupe_key=job.dedupe_key,
                    run_id=job.run_id,
                    outcome_status=job.outcome_status.value,
                    eligible_for_success_mining=job.eligible_for_success_mining,
                    pipeline_version=job.pipeline_version,
                    state=LearningJobState.PENDING.value,
                    attempts=0,
                    correlation_id=job.correlation_id,
                    created_at=job.created_at,
                    updated_at=job.created_at,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "job_type", "dedupe_key"])
            )
            return EnqueueResult.ENQUEUED if created.rowcount == 1 else EnqueueResult.DUPLICATE_JOB

    def record_dead_letter(
        self,
        *,
        consumer: str,
        event_id: str | None,
        tenant_id: str | None,
        reason: str,
        raw: str,
        attempts: int,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                t.dead_letters.insert().values(
                    dead_letter_id=new_id(),
                    consumer=consumer,
                    event_id=event_id,
                    tenant_id=tenant_id,
                    reason=reason[:1000],
                    raw=raw[:4096],
                    attempts=attempts,
                    created_at=datetime.now(UTC),
                )
            )

    def count_jobs(self, tenant_id: str, run_id: str) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(t.learning_jobs)
                    .where(
                        t.learning_jobs.c.tenant_id == tenant_id, t.learning_jobs.c.run_id == run_id
                    )
                ).scalar_one()
            )
