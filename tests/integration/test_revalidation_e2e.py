"""M6.3 integration (real PostgreSQL): revalidation evidence is persisted atomically, append-only,
tenant scoped and replayable; a restoration is reproducible from the stored evidence and the policy
version; SHADOW never executes; tampering with the configuration is detected."""

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from test_drift_e2e import DEFAULT, KEY, TENANT, drift_definition

from controller_support import PLANNING_KEY
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db.controller_store import SqlControllerPolicyStore
from experience_engine.adapters.db.drift_store import SqlDriftLog
from experience_engine.adapters.db.revalidation_store import SqlRevalidationLog
from experience_engine.controller.drift_service import DriftService
from experience_engine.controller.revalidation_service import (
    RevalidationReplayMismatch,
    RevalidationService,
)
from experience_engine.domain.controller import (
    ControllerParams,
    ControllerPolicyDefinition,
    DecisionOutcomeRecord,
)
from experience_engine.domain.drift import DriftState
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.revalidation import (
    RevalState,
    RevalTrigger,
    TrialVerdict,
)
from revalidation_support import Script

pytestmark = pytest.mark.integration
T0 = datetime(2026, 1, 1, tzinfo=UTC)
S, F = TrialVerdict.SUCCESS, TrialVerdict.FAILURE


def reval_definition(
    version: int, mode: str = "ENFORCE", strategy: str = "FIXED", **rv: Any
) -> ControllerPolicyDefinition:
    base = drift_definition(version, mode)
    raw = base.params.model_dump(mode="json")
    raw["revalidation"] = {"strategy": strategy, "dwell": 10, **rv}
    return base.model_copy(update={"params": ControllerParams.model_validate(raw)})


def record(i: int, action: str, ok: bool, definition: ControllerPolicyDefinition) -> Any:
    return DecisionOutcomeRecord(
        decision_id=f"d-{i:05d}",
        run_id=f"r-{i}",
        tenant_id=TENANT,
        policy_version=definition.policy_version,
        selection_probability=1.0,
        family="pool",
        risk_level="low",
        action=action,
        status=OutcomeStatus.VERIFIED_SUCCESS if ok else OutcomeStatus.FAILURE,
        tokens=3000 if action == PLANNING_KEY else 9000,
        default_key=DEFAULT,
        observed_at=T0 + timedelta(hours=i),
        runtime_version=definition.snapshot.prior_sources[0].runtime_version,
        verifier_version=definition.snapshot.prior_sources[0].verifier_version,
    )


def history(definition: ControllerPolicyDefinition) -> list[Any]:
    """The alternative collapses (quarantine), then the rule default serves for a while."""
    rng = random.Random(7)
    recs = [record(i, PLANNING_KEY, rng.random() < 0.05, definition) for i in range(150)]
    return recs + [record(150 + i, DEFAULT, i % 2 == 0, definition) for i in range(40)]


class StepClock:
    """The learning plane's clock in the simulated history: each call is one round, 10 h later."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        self.t += timedelta(hours=10)
        return self.t


def services(engine: Engine, records: list[Any]) -> tuple[RevalidationService, SqlRevalidationLog]:
    log = SqlRevalidationLog(engine)
    svc = RevalidationService(
        lambda tenant, versions: [r for r in records if r.tenant_id == tenant],
        log,
        SqlDriftLog(engine),
        StepClock(T0 + timedelta(hours=190)).now,
    )
    return svc, log


def working_script(d: ControllerPolicyDefinition) -> Script:
    src = d.snapshot.prior_sources[0]
    return Script(S, pool=500, tokens=3000, tenant=TENANT, runtime=src.runtime_version,
                  verifier=src.verifier_version)  # fmt: skip


def registered(engine: Engine, d: ControllerPolicyDefinition) -> ControllerPolicyDefinition:
    SqlControllerPolicyStore(engine).add_version(d, SystemClock().now())
    return d


def test_a_quarantine_is_restored_only_through_persisted_replayable_evidence(
    engine: Engine,
) -> None:
    d = registered(engine, reval_definition(1))
    records = history(d)
    svc, log = services(engine, records)
    first = svc.sync(d)  # execution-plane entry: persists the case, never runs a trial
    assert first.overlay is not None and KEY in first.overlay.cells
    rows = log.transitions(TENANT, "adaptive", 1)
    assert [r.trigger for r in rows] == [RevalTrigger.CASE_OPENED]
    assert log.trials(TENANT, "adaptive", 1) == []
    assert any(
        x.to_state == DriftState.QUARANTINED
        for x in SqlDriftLog(engine).transitions(TENANT, "adaptive", 1)
    )  # the M6.2 audit table is kept in step

    script = working_script(d)
    run = None
    for _ in range(40):
        run = svc.run(d, script)
        state = svc.replay(d).fleet.cases[KEY].state  # type: ignore[union-attr]
        held = KEY in (run.overlay.cells if run.overlay else {})
        assert held == (state != RevalState.RECOVERED)  # prior-disabled until restored
        if state == RevalState.RECOVERED:
            break
    assert run is not None and run.overlay is not None and KEY not in run.overlay.cells
    stored_t = log.transitions(TENANT, "adaptive", 1)
    stored_trials = log.trials(TENANT, "adaptive", 1)
    assert [r.trigger for r in stored_t][-1] == RevalTrigger.CONFIRMED
    assert RevalState.RECOVERED in {r.to_state for r in stored_t}
    recovered = next(r for r in stored_t if r.to_state == RevalState.RECOVERED)
    assert recovered.measurements["n"] == recovered.measurements["plan_n"]  # the whole fixed sample
    assert recovered.params_hash == d.params.revalidation.config_hash()  # type: ignore[union-attr]
    assert stored_trials and all(t.accepted for t in stored_trials)

    # reproducible from the stored evidence and the policy version alone
    again = svc.replay(d)
    assert again.new_transitions == [] and again.fleet is not None
    assert [t.model_dump(mode="json") for t in again.fleet.transitions] == [
        t.model_dump(mode="json") for t in stored_t
    ]
    assert again.overlay is not None and KEY not in again.overlay.cells
    sync = svc.sync(d)  # and a second sync appends nothing
    assert len(log.transitions(TENANT, "adaptive", 1)) == len(stored_t)
    assert sync.overlay is not None and KEY not in sync.overlay.cells


def test_the_tables_are_append_only_and_tenant_scoped(engine: Engine) -> None:
    d = registered(engine, reval_definition(1))
    svc, log = services(engine, history(d))
    svc.sync(d)
    svc.run(d, working_script(d))
    assert log.trials(TENANT, "adaptive", 1)  # there is evidence for the triggers to protect
    assert log.transitions("someone-else", "adaptive", 1) == []
    assert log.trials("someone-else", "adaptive", 1) == []
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE controller_revalidation_transitions SET reason = 'UNKNOWN'"))
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM controller_revalidation_transitions"))
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE controller_revalidation_trials SET tokens = 0"))
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM controller_revalidation_trials"))


def test_evidence_is_checked_by_the_database_too(engine: Engine) -> None:
    d = registered(engine, reval_definition(1))
    row = (
        "INSERT INTO controller_revalidation_trials (trial_id, tenant_id, policy_id, version, "
        "cell_key, episode, cycle, stage, ordinal, anchor, task_ref, verdict, tokens, observed_at, "
        "accepted, reject_reason, created_at) VALUES (gen_random_uuid(), :t, 'adaptive', :v, 'c', "
        "1, 0, 'EVIDENCE', :o, 'a', 'x', :verdict, 10, now(), :acc, :why, now())"
    )
    ok = {"t": TENANT, "v": 1, "o": 0, "verdict": "SUCCESS", "acc": True, "why": None}
    with engine.begin() as conn:
        conn.execute(sa.text(row), ok)
    for bad in (
        {**ok, "o": 0},  # the same trial twice
        {**ok, "o": 1, "verdict": "MAYBE"},  # not a verdict
        {**ok, "o": 1, "acc": False, "why": None},  # a rejection must say why
        {**ok, "o": 1, "acc": True, "why": "DUPLICATE"},  # an accepted trial has no rejection
        {**ok, "o": 1, "v": 9},  # unknown policy version
    ):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(row), bad)
    assert d.version == 1


def test_a_changed_configuration_is_detected_instead_of_silently_recomputed(engine: Engine) -> None:
    d = registered(engine, reval_definition(1))
    svc, _ = services(engine, history(d))
    svc.sync(d)
    svc.run(d, working_script(d))
    tampered = d.model_copy(
        update={
            "params": ControllerParams.model_validate(
                {
                    **d.params.model_dump(mode="json"),
                    "revalidation": {
                        **d.params.revalidation.model_dump(mode="json"),  # type: ignore[union-attr]
                        "alpha": 0.10,
                    },
                }
            )
        }
    )
    with pytest.raises(RevalidationReplayMismatch):
        svc.replay(tampered)


def test_shadow_mode_records_readiness_but_never_executes_or_enforces(engine: Engine) -> None:
    d = registered(engine, reval_definition(1, mode="SHADOW"))
    svc, log = services(engine, history(d))
    script = Script(S, pool=500, tenant=TENANT)
    res = svc.sync(d)
    assert res.overlay is None  # a SHADOW monitor never publishes an overlay
    run = svc.run(d, script)
    assert script.calls == 0 and run.new_trials == [] and run.tokens == 0
    rows = log.transitions(TENANT, "adaptive", 1)
    assert rows and all(not r.applied for r in rows)
    assert RevalState.RECOVERED not in {r.to_state for r in rows}


def test_policies_keep_their_own_service_and_the_old_one_refuses_new_ones(engine: Engine) -> None:
    plain = registered(engine, drift_definition(1, "ENFORCE"))
    revalidating = registered(engine, reval_definition(2))
    records = history(plain)
    drift_log = SqlDriftLog(engine)
    old = DriftService(lambda t, v: records, drift_log, SystemClock().now)
    with pytest.raises(ValueError, match="revalidation"):
        old.replay(revalidating)
    svc = RevalidationService(
        lambda t, v: records, SqlRevalidationLog(engine), drift_log, SystemClock().now, fallback=old
    )
    assert svc.sync(plain).overlay is not None  # delegated: the M6.2 behaviour is unchanged


# ---- real execution path: forced action, independent verifier, nothing written as production -----
def test_trials_run_through_the_orchestrator_and_verifier_without_creating_production_runs(
    engine: Engine,
) -> None:
    from test_controller_e2e import CONFIG, TickClock, pool_tasks

    from experience_engine.adapters.llm.fake import HeuristicFakeLLM
    from experience_engine.bench.environment import SimulatedEnvironment
    from experience_engine.bench.verifier import SimulationVerifier
    from experience_engine.controller.drift import parse_paired_key, references_from_snapshot
    from experience_engine.controller.revalidation_runtime import (
        FORCED_POLICY_VERSION,
        ForcedController,
        OrchestratorTrialSource,
        RevalidationTask,
    )
    from experience_engine.execution.orchestrator import TaskOrchestrator

    d = drift_definition(1, "ENFORCE")
    ref = references_from_snapshot(d.snapshot)[0]
    assert parse_paired_key(ref.cell_key)[3] == PLANNING_KEY
    pool = [
        RevalidationTask(
            b.task,
            lambda b=b: SimulatedEnvironment(b.environment, b.task.allowed_tools),  # type: ignore[misc]
            HeuristicFakeLLM,
        )
        for b in pool_tasks(4)
    ]
    verifier = SimulationVerifier()
    source = OrchestratorTrialSource(
        tenant_id=TENANT,
        tasks=pool,
        build=lambda c: TaskOrchestrator(  # no lifecycle: nothing is persisted as a run
            controller=c, verifier=verifier, clock=TickClock(), config=CONFIG
        ),
        runtime_version=ref.runtime_version or "rt",
        verifier_version=verifier.version,
    )
    facts = source.facts(ref, T0)
    assert facts.tenant_id == TENANT and facts.pool == 4 and facts.verification_available
    results = [source.run(ref, key_of(i), T0 + timedelta(hours=i)) for i in range(4)]
    assert all(r is not None for r in results)
    assert {r.verdict for r in results if r} <= set(TrialVerdict)
    assert all(r.capability_ref == PLANNING_KEY and r.tenant_id == TENANT for r in results if r)
    assert all(r.verifier_version == verifier.version for r in results if r)
    assert all(r.tokens > 0 for r in results if r)  # real, measured model tokens
    assert source.run(ref, key_of(9), T0) is None  # the pool is finite: no result, no invention
    with engine.connect() as conn:  # the production tables never saw a trial
        for table in ("tasks", "task_runs", "run_outcomes", "policy_decisions", "outbox_events"):
            assert conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    assert ForcedController(PLANNING_KEY).policy_version == FORCED_POLICY_VERSION


def test_a_foreign_tenants_tasks_are_never_in_the_pool() -> None:
    from test_controller_e2e import CONFIG, TickClock, pool_tasks

    from experience_engine.adapters.llm.fake import HeuristicFakeLLM
    from experience_engine.bench.environment import SimulatedEnvironment
    from experience_engine.bench.verifier import SimulationVerifier
    from experience_engine.controller.drift import references_from_snapshot
    from experience_engine.controller.revalidation_runtime import (
        OrchestratorTrialSource,
        RevalidationTask,
    )
    from experience_engine.execution.orchestrator import TaskOrchestrator

    ref = references_from_snapshot(drift_definition(1, "ENFORCE").snapshot)[0]
    own = pool_tasks(2)
    foreign = [
        RevalidationTask(
            b.task.model_copy(update={"tenant_id": "other"}),
            lambda b=b: SimulatedEnvironment(b.environment, b.task.allowed_tools),  # type: ignore[misc]
            HeuristicFakeLLM,
        )
        for b in pool_tasks(3)
    ]
    mine = [
        RevalidationTask(
            b.task,
            lambda b=b: SimulatedEnvironment(b.environment, b.task.allowed_tools),  # type: ignore[misc]
            HeuristicFakeLLM,
        )
        for b in own
    ]
    verifier = SimulationVerifier()
    source = OrchestratorTrialSource(
        tenant_id=TENANT,
        tasks=[*foreign, *mine],
        build=lambda c: TaskOrchestrator(
            controller=c, verifier=verifier, clock=TickClock(), config=CONFIG
        ),
        runtime_version="rt",
        verifier_version=verifier.version,
    )
    assert source.facts(ref, T0).pool == 2


def key_of(i: int) -> Any:
    from experience_engine.domain.revalidation import TrialKey

    return TrialKey(cell_key=KEY, episode=1, cycle=0, ordinal=i)


def test_prefetched_trials_are_served_in_order_stamped_when_consumed_and_disclosed_if_unused() -> (
    None
):
    from test_controller_e2e import CONFIG, TickClock, pool_tasks

    from experience_engine.adapters.llm.fake import HeuristicFakeLLM
    from experience_engine.bench.environment import SimulatedEnvironment
    from experience_engine.bench.verifier import SimulationVerifier
    from experience_engine.controller.drift import references_from_snapshot
    from experience_engine.controller.revalidation_runtime import (
        OrchestratorTrialSource,
        RevalidationTask,
    )
    from experience_engine.execution.orchestrator import TaskOrchestrator

    ref = references_from_snapshot(drift_definition(1, "ENFORCE").snapshot)[0]
    pool = [
        RevalidationTask(
            b.task,
            lambda b=b: SimulatedEnvironment(b.environment, b.task.allowed_tools),  # type: ignore[misc]
            HeuristicFakeLLM,
        )
        for b in pool_tasks(6)
    ]
    verifier = SimulationVerifier()

    def make(workers: int) -> OrchestratorTrialSource:
        return OrchestratorTrialSource(
            tenant_id=TENANT,
            tasks=pool,
            build=lambda c: TaskOrchestrator(
                controller=c, verifier=verifier, clock=TickClock(), config=CONFIG
            ),
            runtime_version="rt",
            verifier_version=verifier.version,
            workers=workers,
        )

    serial, parallel = make(1), make(4)
    a = [serial.run(ref, key_of(i), T0 + timedelta(hours=i)) for i in range(2)]
    b = [parallel.run(ref, key_of(i), T0 + timedelta(hours=i)) for i in range(2)]
    assert [r.task_ref for r in a if r] == [r.task_ref for r in b if r]  # same tasks, same order
    assert [r.verdict for r in a if r] == [r.verdict for r in b if r]  # same independent verdicts
    assert [r.observed_at for r in b if r] == [
        T0,
        T0 + timedelta(hours=1),
    ]  # stamped on consumption
    assert parallel.facts(ref, T0).pool == 4  # 6 tasks − 2 consumed; 2 of them already executed
    unused = parallel.close()
    assert unused > 0  # the two executed-but-unrecorded trials are disclosed as cost
    assert serial.close() == 0

    stamped = make(1)  # a real executor stamps its own observation time, not the nominal slot
    stamped._clock = lambda: T0 + timedelta(days=9)
    got = stamped.run(ref, key_of(0), T0 + timedelta(days=30))
    assert got is not None and got.observed_at == T0 + timedelta(days=9)
