"""Integration tests against a real PostgreSQL (see tests/conftest.py)."""

import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.reader import SqlRunReader
from experience_engine.adapters.db.run_lifecycle import (
    RunStateError,
    SqlRunLifecycle,
    TaskSpecConflictError,
)
from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.redaction import Redactor
from experience_engine.domain.run import (
    EXECUTION_COMPLETED,
    ExecutionCompletedPayload,
    OutboxEvent,
    OutboxStatus,
)
from experience_engine.domain.trace import TraceEventType
from experience_engine.ports.artifacts import ArtifactNotFoundError
from support import FakeClock, correct_pool_fix, first_task, run_persisted

pytestmark = pytest.mark.integration


@pytest.fixture
def lifecycle(engine: Engine) -> SqlRunLifecycle:
    return SqlRunLifecycle(engine)


@pytest.fixture
def artifacts(tmp_path: Path) -> LocalFsArtifactStore:
    return LocalFsArtifactStore(tmp_path / "artifacts")


def _scalar(engine: Engine, query: sa.Select[tuple[int]]) -> int:
    with engine.connect() as conn:
        return int(conn.execute(query).scalar_one())


def test_run_can_be_reconstructed_from_events_and_artifacts(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    result = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")

    rec = SqlRunReader(engine).reconstruct("acme", result.run_id)
    assert rec is not None
    assert rec.run.status.value == "COMPLETED" and rec.run.correlation_id == result.run_id
    assert rec.run.ended_at is not None and rec.task.task_id == task.task.task_id
    assert [e.type for e in rec.events] == [
        TraceEventType.DECISION,
        TraceEventType.MODEL_CALL,
        TraceEventType.TOOL_CALL,
        TraceEventType.MODEL_CALL,
        TraceEventType.RUN_TERMINATED,
        TraceEventType.VERIFICATION,
    ]
    assert [e.sequence for e in rec.events] == list(range(6))
    by_id = {e.event_id: e for e in rec.events}
    assert all(e.parent_event_id is None or e.parent_event_id in by_id for e in rec.events)

    # Why did the agent act? The prompt it saw, its reply, the tool args and the observation.
    model_call, tool_call = rec.events[1], rec.events[2]
    request = json.loads(artifacts.get("acme", _ref(model_call, "request")))
    assert request[0]["role"] == "system" and "JSON" in request[0]["content"]
    assert (
        json.loads(artifacts.get("acme", _ref(model_call, "response")))["tool"] == "set_json_value"
    )
    assert json.loads(artifacts.get("acme", _ref(tool_call, "args")))["key"] == "database.pool_size"
    assert artifacts.get("acme", _ref(tool_call, "output")).startswith(b"set database.pool_size")
    verification = rec.events[-1]
    assert json.loads(artifacts.get("acme", _ref(verification, "results")))[0]["passed"] is True

    assert rec.outcome is not None and rec.outcome.status == OutcomeStatus.VERIFIED_SUCCESS
    assert rec.outcome == result.outcome.model_copy(
        update={"verification_results": rec.outcome.verification_results}
    )
    (decision,) = rec.decisions
    assert decision.outcome_status == OutcomeStatus.VERIFIED_SUCCESS
    assert decision.eligible_actions == result.decision.eligible_actions
    assert {e.component_version for e in rec.events if e.type == TraceEventType.DECISION} == {
        decision.policy_version
    }

    with engine.connect() as conn:
        actions = (
            conn.execute(sa.select(t.audit_log.c.action).order_by(t.audit_log.c.ts)).scalars().all()
        )
        outbox = conn.execute(sa.select(t.outbox_events)).mappings().all()
    assert actions == ["run.started", "run.completed"]
    (row,) = outbox
    assert row["status"] == "PENDING" and row["type"] == EXECUTION_COMPLETED
    assert ExecutionCompletedPayload.model_validate(row["payload"]).run_id == result.run_id


def _ref(event, role: str) -> str:  # type: ignore[no-untyped-def]
    return next(r.sha256 for r in event.artifact_refs if r.role == role)


def test_completion_is_atomic_outcome_run_status_and_outbox_together(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    good = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    with engine.connect() as conn:
        existing_outbox_id = conn.execute(sa.select(t.outbox_events.c.event_id)).scalar_one()

    # Second run whose completion collides with an existing outbox event id: nothing may commit.
    class CollidingLifecycle(SqlRunLifecycle):
        def complete_run(self, outcome, decision, ended_at, outbox_event):  # type: ignore[no-untyped-def]
            super().complete_run(
                outcome,
                decision,
                ended_at,
                outbox_event.model_copy(update={"event_id": existing_outbox_id}),
            )

    with pytest.raises(IntegrityError):
        run_persisted(
            task, correct_pool_fix(task), CollidingLifecycle(engine), artifacts, tenant="acme"
        )

    with engine.connect() as conn:
        runs = conn.execute(sa.select(t.task_runs.c.run_id, t.task_runs.c.status)).all()
        outcomes = conn.execute(sa.select(t.run_outcomes.c.run_id)).scalars().all()
        pending = conn.execute(sa.select(t.policy_decisions.c.outcome_status)).scalars().all()
    stuck = [r for r in runs if r.run_id != good.run_id]
    assert len(stuck) == 1 and stuck[0].status == "RUNNING"  # partial trace stays inspectable
    assert outcomes == [good.run_id]
    assert sorted(map(str, pending)) == ["None", "VERIFIED_SUCCESS"]
    assert _scalar(engine, sa.select(sa.func.count()).select_from(t.outbox_events)) == 1


def test_complete_run_rejects_unknown_run_and_double_completion(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    result = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    event = OutboxEvent(
        event_id=new_id(),
        tenant_id="acme",
        type=EXECUTION_COMPLETED,
        aggregate_id=result.run_id,
        payload={},
        correlation_id="c",
        created_at=result.ended_at,
    )
    with pytest.raises(RunStateError):
        lifecycle.complete_run(result.outcome, result.decision, result.ended_at, event)
    other_tenant = result.outcome.model_copy(update={"tenant_id": "evil"})
    other_decision = result.decision.model_copy(update={"tenant_id": "evil"})
    with pytest.raises(RunStateError):
        lifecycle.complete_run(
            other_tenant,
            other_decision,
            result.ended_at,
            event.model_copy(update={"tenant_id": "evil"}),
        )


def test_task_spec_is_immutable_per_tenant(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    changed = task.model_copy(
        update={"task": task.task.model_copy(update={"objective": "different"})}
    )
    with pytest.raises(TaskSpecConflictError):
        run_persisted(changed, [], lifecycle, artifacts, tenant="acme")
    # Same task_id under another tenant is a different, independent task.
    run_persisted(changed, [{"final": "x"}], lifecycle, artifacts, tenant="globex")


def test_append_only_tables_reject_update_and_delete(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    statements = [
        "UPDATE trace_events SET status = 'x'",
        "DELETE FROM trace_events",
        "UPDATE run_outcomes SET status = 'FAILURE'",
        "DELETE FROM run_outcomes",
        "UPDATE audit_log SET actor = 'x'",
        "DELETE FROM audit_log",
        "UPDATE artifacts SET uri = 'x'",
        "DELETE FROM artifacts",
        "DELETE FROM policy_decisions",
        "UPDATE policy_decisions SET outcome_status = 'FAILURE'",  # already back-filled
        "UPDATE policy_decisions SET reason = 'rewritten'",
    ]
    for stmt in statements:
        with (
            pytest.raises(DBAPIError, match="append-only|back-fill|cannot be deleted"),
            engine.begin() as conn,
        ):
            conn.execute(sa.text(stmt))
    assert _scalar(engine, sa.select(sa.func.count()).select_from(t.trace_events)) == 6


def test_decision_outcome_can_be_backfilled_once_only(
    engine: Engine, lifecycle: SqlRunLifecycle
) -> None:
    from datetime import UTC, datetime

    from experience_engine.domain.decision import ControllerDecision, Strategy
    from experience_engine.domain.run import RunRecord

    task = first_task().task.model_copy(update={"tenant_id": "acme"})
    run_id = new_id()
    now = datetime.now(UTC)
    lifecycle.start_run(
        task,
        RunRecord(
            run_id=run_id,
            tenant_id="acme",
            task_id=task.task_id,
            correlation_id=run_id,
            config_id="A",
            policy_version="p",
            runtime_version="r",
            started_at=now,
        ),
    )
    decision = ControllerDecision(
        decision_id=new_id(),
        run_id=run_id,
        tenant_id="acme",
        policy_version="p",
        feature_snapshot={},
        eligible_actions=[Strategy.GENERAL_PLANNING],
        selected_action=Strategy.GENERAL_PLANNING,
        selection_probability=1.0,
        reason="r",
    )
    lifecycle.record_decision(decision)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE policy_decisions SET outcome_status = 'FAILURE' WHERE decision_id = :d"
            ),
            {"d": decision.decision_id},
        )
    with pytest.raises(DBAPIError, match="one-time"), engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE policy_decisions SET outcome_status = 'VERIFIED_SUCCESS' "
                "WHERE decision_id = :d"
            ),
            {"d": decision.decision_id},
        )


def test_tenant_isolation_in_reads_artifacts_and_foreign_keys(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    a = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    b = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="globex")
    reader = SqlRunReader(engine)
    assert reader.reconstruct("globex", a.run_id) is None
    assert reader.reconstruct("acme", b.run_id) is None
    rec_a = reader.reconstruct("acme", a.run_id)
    assert rec_a is not None and {e.tenant_id for e in rec_a.events} == {"acme"}

    sha = rec_a.events[1].artifact_refs[0].sha256
    assert artifacts.get("acme", sha)
    with pytest.raises(ArtifactNotFoundError):  # globex stored the same bytes under its own scope
        artifacts.get("initech", sha)

    # A child row cannot point at another tenant's run, even by direct SQL.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            t.trace_events.insert().values(
                event_id=new_id(),
                tenant_id="globex",
                task_id=task.task.task_id,
                run_id=a.run_id,
                trace_id="x",
                sequence=99,
                type="DECISION",
                component="c",
                component_version="1",
                action="a",
                status="s",
                metrics={},
                artifact_refs=[],
                ts=sa.func.now(),
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            t.learning_jobs.insert().values(
                job_id=new_id(),
                tenant_id="globex",
                job_type="x",
                dedupe_key="k",
                run_id=a.run_id,
                outcome_status="FAILURE",
                eligible_for_success_mining=False,
                pipeline_version="p",
                state="PENDING",
                attempts=0,
                correlation_id="c",
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )


def test_traces_contain_no_secrets(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore, tmp_path: Path
) -> None:
    task = first_task()
    known = "llm-provider-key-0123456789"
    replies = [
        {
            "tool": "set_json_value",
            "args": {
                "path": "app/config.json",
                "key": "database.pool_size",
                "value": "sk-live-ABCDEF1234567890XYZ",
            },
        },
        {"tool": "read_file", "args": {"path": "app/config.json"}},
        {"final": f"done, password=hunter2hunter2 {known}"},
    ]
    run_persisted(task, replies, lifecycle, artifacts, tenant="acme", redactor=Redactor([known]))
    with engine.connect() as conn:
        dump = json.dumps(
            [
                [dict(r) for r in conn.execute(sa.select(t.trace_events)).mappings()],
                [dict(r) for r in conn.execute(sa.select(t.audit_log)).mappings()],
                [dict(r) for r in conn.execute(sa.select(t.outbox_events)).mappings()],
                [dict(r) for r in conn.execute(sa.select(t.run_outcomes)).mappings()],
            ],
            default=str,
        )
    files = "".join(
        p.read_text(errors="replace") for p in (tmp_path / "artifacts").rglob("*") if p.is_file()
    )
    for blob in (dump, files):
        for secret in ("sk-live-ABCDEF1234567890XYZ", "hunter2hunter2", known):
            assert secret not in blob
    assert "[REDACTED]" in files


def test_database_rejects_unbounded_trace_rows(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    result = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    with pytest.raises(IntegrityError, match="metrics_bounded"), engine.begin() as conn:
        conn.execute(
            t.trace_events.insert().values(
                event_id=new_id(),
                tenant_id="acme",
                task_id=task.task.task_id,
                run_id=result.run_id,
                trace_id="x",
                sequence=500,
                type="DECISION",
                component="c",
                component_version="1",
                action="a",
                status="s",
                metrics={"blob": "x" * 20_000},
                artifact_refs=[],
                ts=sa.func.now(),
            )
        )


def test_duplicate_event_sequence_is_rejected(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    task = first_task()
    result = run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme")
    rec = SqlRunReader(engine).reconstruct("acme", result.run_id)
    assert rec is not None
    dup = rec.events[0].model_copy(update={"event_id": new_id()})
    with pytest.raises(IntegrityError):
        lifecycle.append_event(dup)


def test_harness_persists_every_run(
    engine: Engine, lifecycle: SqlRunLifecycle, artifacts: LocalFsArtifactStore
) -> None:
    from experience_engine.adapters.llm.fake import HeuristicFakeLLM
    from experience_engine.adapters.results import InMemoryResultStore
    from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
    from experience_engine.bench.harness import run_benchmark
    from experience_engine.bench.verifier import SimulationVerifier
    from experience_engine.domain.benchmark import DatasetVersion, Split
    from experience_engine.execution.controller import FixedPolicy
    from support import config

    tasks = generate_split(Split.DISCOVERY, DEFAULT_SEED)[:3]
    records = run_benchmark(
        tasks,
        dataset=DatasetVersion(
            name="d", version="1", split=Split.DISCOVERY, content_hash="h" * 64, n_tasks=3
        ),
        seed=0,
        config=config(),
        llm_factory=lambda _t, _s: HeuristicFakeLLM(),
        controller=FixedPolicy(),
        verifier=SimulationVerifier(),
        clock=FakeClock(),
        store=InMemoryResultStore(),
        lifecycle=lifecycle,
        artifacts=artifacts,
    )
    assert len(records) == 3
    for table in (t.run_outcomes, t.outbox_events, t.policy_decisions):
        assert _scalar(engine, sa.select(sa.func.count()).select_from(table)) == 3
    assert (
        _scalar(
            engine,
            sa.select(sa.func.count())
            .select_from(t.outbox_events)
            .where(t.outbox_events.c.status == OutboxStatus.PENDING.value),
        )
        == 3
    )
    assert uuid.UUID(records[0].run_id).version == 7
