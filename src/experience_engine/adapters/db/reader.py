import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.outcome import RunOutcome
from experience_engine.domain.run import RunRecord
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.trace import TraceEvent
from experience_engine.ports.persistence import RunReconstruction


class SqlRunReader:
    """Read side. Every query filters on tenant_id; a foreign tenant's run looks non-existent."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def reconstruct(self, tenant_id: str, run_id: str) -> RunReconstruction | None:
        with self._engine.connect() as conn:
            run_row = (
                conn.execute(
                    sa.select(t.task_runs).where(
                        t.task_runs.c.tenant_id == tenant_id, t.task_runs.c.run_id == run_id
                    )
                )
                .mappings()
                .first()
            )
            if run_row is None:
                return None
            task_row = (
                conn.execute(
                    sa.select(t.tasks.c.spec).where(
                        t.tasks.c.tenant_id == tenant_id,
                        t.tasks.c.task_id == run_row["task_id"],
                    )
                )
                .mappings()
                .one()
            )
            events = (
                conn.execute(
                    sa.select(t.trace_events)
                    .where(
                        t.trace_events.c.tenant_id == tenant_id, t.trace_events.c.run_id == run_id
                    )
                    .order_by(t.trace_events.c.sequence)
                )
                .mappings()
                .all()
            )
            decisions = (
                conn.execute(
                    sa.select(t.policy_decisions)
                    .where(
                        t.policy_decisions.c.tenant_id == tenant_id,
                        t.policy_decisions.c.run_id == run_id,
                    )
                    .order_by(t.policy_decisions.c.created_at)
                )
                .mappings()
                .all()
            )
            outcome_row = (
                conn.execute(
                    sa.select(t.run_outcomes).where(
                        t.run_outcomes.c.tenant_id == tenant_id, t.run_outcomes.c.run_id == run_id
                    )
                )
                .mappings()
                .first()
            )

        outcome = None
        if outcome_row is not None:
            data = {k: v for k, v in outcome_row.items() if k != "created_at"}
            outcome = RunOutcome.model_validate(data)
        return RunReconstruction(
            run=RunRecord.model_validate(dict(run_row)),
            task=TaskSpec.model_validate(task_row["spec"]),
            events=[TraceEvent.model_validate(dict(e)) for e in events],
            decisions=[
                ControllerDecision.model_validate({k: v for k, v in d.items() if k != "created_at"})
                for d in decisions
            ],
            outcome=outcome,
        )
