from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.domain.run import LearningJob, LearningJobState
from experience_engine.ports.experience import ClaimedJob

DEFAULT_LEASE = timedelta(minutes=10)


class SqlLearningJobQueue:
    """Claims learning jobs with FOR UPDATE SKIP LOCKED and a lease: a worker that crashes
    while a job is RUNNING simply lets the lease expire and the job is claimed again.
    """

    def __init__(self, engine: Engine, lease: timedelta = DEFAULT_LEASE):
        self._engine = engine
        self._lease = lease

    def claim_pending(self, job_type: str, batch: int, now: datetime) -> list[ClaimedJob]:
        jobs = t.learning_jobs
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(jobs)
                    .where(
                        jobs.c.job_type == job_type,
                        sa.or_(
                            jobs.c.state == LearningJobState.PENDING.value,
                            sa.and_(
                                jobs.c.state == LearningJobState.RUNNING.value,
                                jobs.c.updated_at < now - self._lease,
                            ),
                        ),
                    )
                    .order_by(jobs.c.created_at, jobs.c.job_id)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            claimed: list[ClaimedJob] = []
            for r in rows:
                conn.execute(
                    jobs.update()
                    .where(jobs.c.job_id == r["job_id"])
                    .values(
                        state=LearningJobState.RUNNING.value,
                        attempts=r["attempts"] + 1,
                        updated_at=now,
                    )
                )
                claimed.append(
                    ClaimedJob(
                        job=LearningJob(
                            job_id=r["job_id"],
                            tenant_id=r["tenant_id"],
                            job_type=r["job_type"],
                            dedupe_key=r["dedupe_key"],
                            run_id=r["run_id"],
                            outcome_status=r["outcome_status"],
                            eligible_for_success_mining=r["eligible_for_success_mining"],
                            pipeline_version=r["pipeline_version"],
                            correlation_id=r["correlation_id"],
                            created_at=r["created_at"],
                        ),
                        attempts=r["attempts"] + 1,
                    )
                )
        return claimed

    def mark_succeeded(self, tenant_id: str, job_id: str, now: datetime) -> None:
        self._set(tenant_id, job_id, LearningJobState.SUCCEEDED, None, now)

    def mark_retry_or_failed(
        self, tenant_id: str, job_id: str, error: str, give_up: bool, now: datetime
    ) -> None:
        state = LearningJobState.FAILED if give_up else LearningJobState.PENDING
        self._set(tenant_id, job_id, state, error, now)

    def _set(
        self, tenant_id: str, job_id: str, state: LearningJobState, error: str | None, now: datetime
    ) -> None:
        jobs = t.learning_jobs
        with self._engine.begin() as conn:
            conn.execute(
                jobs.update()
                .where(jobs.c.job_id == job_id, jobs.c.tenant_id == tenant_id)
                .values(state=state.value, last_error=error, updated_at=now)
            )
