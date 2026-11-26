import hashlib
import json
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from experience_engine.adapters.db import tables as t
from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import RunOutcome
from experience_engine.domain.run import OutboxEvent, RunRecord, RunStatus
from experience_engine.domain.task import TaskSpec
from experience_engine.domain.trace import ArtifactRef, TraceEvent

ACTOR = "system:orchestrator"


class TaskSpecConflictError(Exception):
    """A task_id was reused for a different spec; task specs are immutable once recorded."""


class RunStateError(Exception):
    """The run does not exist for this tenant or is not RUNNING."""


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_audit(
    conn: Connection,
    *,
    tenant_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    correlation_id: str,
    ts: datetime,
    detail: dict[str, object] | None = None,
    actor: str = ACTOR,
) -> None:
    conn.execute(
        t.audit_log.insert().values(
            audit_id=new_id(),
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
            correlation_id=correlation_id,
            ts=ts,
        )
    )


class SqlRunLifecycle:
    """RunLifecycle on PostgreSQL. Each public method is its own transaction, except that
    complete_run writes everything about completion atomically (including the outbox row).
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def start_run(self, task: TaskSpec, run: RunRecord) -> None:
        if task.tenant_id != run.tenant_id or task.task_id != run.task_id:
            raise ValueError("task and run disagree on tenant/task id")
        spec = task.model_dump(mode="json")
        digest = spec_hash(spec)
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(t.tasks)
                .values(
                    tenant_id=task.tenant_id,
                    task_id=task.task_id,
                    spec=spec,
                    spec_hash=digest,
                    created_at=run.started_at,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "task_id"])
            )
            existing = conn.execute(
                sa.select(t.tasks.c.spec_hash).where(
                    t.tasks.c.tenant_id == task.tenant_id, t.tasks.c.task_id == task.task_id
                )
            ).scalar_one()
            if existing != digest:
                raise TaskSpecConflictError(
                    f"task {task.task_id!r} already exists with another spec"
                )
            conn.execute(
                t.task_runs.insert().values(
                    run_id=run.run_id,
                    tenant_id=run.tenant_id,
                    task_id=run.task_id,
                    correlation_id=run.correlation_id,
                    config_id=run.config_id,
                    policy_version=run.policy_version,
                    runtime_version=run.runtime_version,
                    analyzer_version=run.analyzer_version,
                    model_settings=run.model_settings,
                    tags=run.tags,
                    status=RunStatus.RUNNING.value,
                    started_at=run.started_at,
                )
            )
            write_audit(
                conn,
                tenant_id=run.tenant_id,
                action="run.started",
                entity_type="task_run",
                entity_id=run.run_id,
                correlation_id=run.correlation_id,
                ts=run.started_at,
                detail={"task_id": run.task_id, "config_id": run.config_id},
            )

    def register_artifact(self, tenant_id: str, ref: ArtifactRef, uri: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(t.artifacts)
                .values(
                    tenant_id=tenant_id,
                    sha256=ref.sha256,
                    uri=uri,
                    size_bytes=ref.size_bytes,
                    content_type=ref.content_type,
                    redacted=ref.redacted,
                    truncated=ref.truncated,
                    created_at=sa.func.now(),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "sha256"])
            )

    def append_event(self, event: TraceEvent) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                t.trace_events.insert().values(
                    event_id=event.event_id,
                    tenant_id=event.tenant_id,
                    task_id=event.task_id,
                    run_id=event.run_id,
                    trace_id=event.trace_id,
                    sequence=event.sequence,
                    parent_event_id=event.parent_event_id,
                    type=event.type.value,
                    component=event.component,
                    component_version=event.component_version,
                    action=event.action,
                    status=event.status,
                    metrics=event.metrics,
                    artifact_refs=[r.model_dump(mode="json") for r in event.artifact_refs],
                    ts=event.ts,
                )
            )

    def record_decision(self, decision: ControllerDecision) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                t.policy_decisions.insert().values(
                    decision_id=decision.decision_id,
                    tenant_id=decision.tenant_id,
                    run_id=decision.run_id,
                    policy_version=decision.policy_version,
                    feature_snapshot=decision.feature_snapshot,
                    eligible_actions=[a.value for a in decision.eligible_actions],
                    estimates=decision.estimates,
                    selected_action=decision.selected_action.value,
                    selection_probability=decision.selection_probability,
                    reason=decision.reason,
                    outcome_status=None,
                    acted=decision.acted,
                    created_at=sa.func.now(),
                )
            )

    def complete_run(
        self,
        outcome: RunOutcome,
        decision: ControllerDecision,
        ended_at: datetime,
        outbox_event: OutboxEvent,
    ) -> None:
        if not (outcome.tenant_id == decision.tenant_id == outbox_event.tenant_id):
            raise ValueError("outcome, decision and outbox event must share a tenant")
        if decision.outcome_status != outcome.status:
            raise ValueError("decision outcome_status must equal the run outcome status")
        with self._engine.begin() as conn:
            updated = conn.execute(
                t.task_runs.update()
                .where(
                    t.task_runs.c.run_id == outcome.run_id,
                    t.task_runs.c.tenant_id == outcome.tenant_id,
                    t.task_runs.c.status == RunStatus.RUNNING.value,
                )
                .values(status=RunStatus.COMPLETED.value, ended_at=ended_at)
            )
            if updated.rowcount != 1:
                raise RunStateError(f"run {outcome.run_id} is not a RUNNING run of this tenant")
            conn.execute(
                t.run_outcomes.insert().values(
                    run_id=outcome.run_id,
                    tenant_id=outcome.tenant_id,
                    task_id=outcome.task_id,
                    config_id=outcome.config_id,
                    policy_version=outcome.policy_version,
                    capability_version=outcome.capability_version,
                    status=outcome.status.value,
                    quality_score=outcome.quality_score,
                    verification_results=[
                        r.model_dump(mode="json") for r in outcome.verification_results
                    ],
                    tokens_used=outcome.tokens_used,
                    tool_calls=outcome.tool_calls,
                    execution_time_ms=outcome.execution_time_ms,
                    human_interventions=outcome.human_interventions,
                    failure_reasons=outcome.failure_reasons,
                    evidence_refs=outcome.evidence_refs,
                    verifier_version=outcome.verifier_version,
                    created_at=ended_at,
                )
            )
            backfilled = conn.execute(
                t.policy_decisions.update()
                .where(
                    t.policy_decisions.c.decision_id == decision.decision_id,
                    t.policy_decisions.c.tenant_id == decision.tenant_id,
                    t.policy_decisions.c.run_id == outcome.run_id,
                    t.policy_decisions.c.outcome_status.is_(None),
                )
                .values(outcome_status=outcome.status.value)
            )
            if backfilled.rowcount != 1:
                raise RunStateError("decision missing or already has a recorded outcome")
            conn.execute(
                t.outbox_events.insert().values(
                    event_id=outbox_event.event_id,
                    tenant_id=outbox_event.tenant_id,
                    type=outbox_event.type,
                    aggregate_id=outbox_event.aggregate_id,
                    payload=outbox_event.payload,
                    correlation_id=outbox_event.correlation_id,
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=outbox_event.created_at,
                    created_at=outbox_event.created_at,
                )
            )
            write_audit(
                conn,
                tenant_id=outcome.tenant_id,
                action="run.completed",
                entity_type="task_run",
                entity_id=outcome.run_id,
                correlation_id=outbox_event.correlation_id,
                ts=ended_at,
                detail={"status": outcome.status.value, "outbox_event_id": outbox_event.event_id},
            )
