"""M6.4 Phase 1 integration (real PostgreSQL): the outage, suspension, evidence and gate decisions
are persisted atomically and replay exactly from the stored evidence; the database and the
service each refuse a recovery no passing gate report authorised; provenance round-trips; an
interrupted round leaves nothing half written; the orchestrator-backed source reports the new
outcome classes, timeout origins and provenance."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from test_drift_e2e import KEY, TENANT
from test_revalidation_e2e import T0, S, history, registered, reval_definition, services

from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.controller.recovery_gates import RecoveryGateViolation
from experience_engine.controller.revalidation import ReplaySource
from experience_engine.domain.controller import ControllerParams, ControllerPolicyDefinition
from experience_engine.domain.outcome import OutcomeStatus, TerminationReason, VerificationResult
from experience_engine.domain.recovery import TimeoutOrigin, TrialProvenance, VerificationStatus
from experience_engine.domain.revalidation import (
    RevalState,
    RevalTrial,
    RevalTrigger,
    TrialKey,
    TrialResult,
    TrialVerdict,
)
from recovery_support import AttestedScript

pytestmark = pytest.mark.integration
U = TrialVerdict.VERIFICATION_UNAVAILABLE
RECOVERY: dict[str, Any] = {
    "accepted_executors": ["exec-1"],
    "accepted_models": {"model-a": ["1"]},
    "accepted_tools": {"sql": ["2"]},
    "accepted_verifiers": {"verifier-x": [SimulationVerifier.version]},
    "accepted_sources": ["revalidation-pool"],
    "evidence_window_hours": 5000.0,  # the simulated clock advances 20 h per worker round
}


def gated(version: int = 1, **rv: Any) -> ControllerPolicyDefinition:
    return reval_definition(version, recovery=RECOVERY, **rv)


class World:
    def __init__(self) -> None:
        self.attempt: TrialVerdict | None = None
        self.down = False


def script(d: ControllerPolicyDefinition, world: World | None = None, **kw: Any) -> AttestedScript:
    w = world or World()
    src = d.snapshot.prior_sources[0]
    kw.setdefault("all_prov", {"task_family": "pool"})
    return AttestedScript(
        lambda _n: w.attempt or S, down=lambda _now: w.down, pool=500, tokens=3000,
        tenant=TENANT, runtime=src.runtime_version, verifier=src.verifier_version, **kw,
    )  # fmt: skip


def recovered(svc: Any, d: ControllerPolicyDefinition, src: Any, limit: int = 40) -> None:
    for _ in range(limit):
        svc.run(d, src)
        if svc.replay(d).fleet.cases[KEY].state == RevalState.RECOVERED:
            return
    raise AssertionError("the case did not recover")


def triggers(log: Any) -> list[str]:
    return [r.trigger.value for r in log.transitions(TENANT, "adaptive", 1)]


# ---- a gated restoration -------------------------------------------------------------------------
def test_a_gated_restoration_is_persisted_with_its_passing_report_and_replays_exactly(
    engine: Engine,
) -> None:
    d = registered(engine, gated())
    svc, log = services(engine, history(d))
    svc.sync(d)
    recovered(svc, d, script(d))
    rows = log.transitions(TENANT, "adaptive", 1)
    done = next(r for r in rows if r.to_state == RevalState.RECOVERED)
    assert done.gates["eligible"] is True and done.gates["failed_gates"] == []
    assert done.gates["policy_version"] == "m6.4-gates-v1" and len(done.gates["passed_gates"]) == 11
    trials = log.trials(TENANT, "adaptive", 1)
    assert set(done.gates["evidence_ids"]) == {t.key.ident() for t in trials if t.accepted}
    assert done.gates["config_hash"] == done.params_hash == d.params.revalidation.config_hash()  # type: ignore[union-attr]
    prov = trials[0].result.provenance  # the provenance came back from the database unchanged
    assert (prov.executor_id, prov.model_id, prov.tool_versions) == (
        "exec-1",
        "model-a",
        {"sql": "2"},
    )
    assert prov.verification_status == VerificationStatus.VERIFIED and prov.task_family == "pool"
    again = svc.replay(d)
    assert again.new_transitions == [] and again.fleet is not None
    assert [t.model_dump(mode="json") for t in again.fleet.transitions] == [
        t.model_dump(mode="json") for t in rows
    ]
    by_key = {t.key.ident(): t.model_dump(mode="json") for t in trials}  # the table sorts by stage
    assert {t.key.ident(): t.model_dump(mode="json") for t in again.fleet.trials} == by_key
    svc.sync(d)
    assert len(log.transitions(TENANT, "adaptive", 1)) == len(rows)  # idempotent


def test_an_outage_is_persisted_as_suspension_and_resume_and_costs_no_cycle(
    engine: Engine,
) -> None:
    d = registered(engine, gated())
    svc, log = services(engine, history(d))
    world = World()
    src = script(d, world)
    svc.sync(d)
    svc.run(d, src)
    svc.run(d, src)  # 100 evidence trials
    world.attempt = U  # the verifier dies; only the attempts reveal it
    svc.run(d, src)
    assert triggers(log)[-1] == "SUSPENDED"
    spent = sum(t.result.tokens for t in log.trials(TENANT, "adaptive", 1))
    world.attempt, world.down = None, True
    for _ in range(3):
        svc.run(d, src)
    assert sum(t.result.tokens for t in log.trials(TENANT, "adaptive", 1)) == spent  # nothing spent
    world.down = False
    recovered(svc, d, src)
    rows = log.transitions(TENANT, "adaptive", 1)
    names = iter(triggers(log))
    for expected in ("SUSPENDED", "OUTAGE_PROBE", "RESUMED", "CRITERIA_MET", "CONFIRMED"):
        assert expected in names, expected
    assert all(r.cycle == 0 for r in rows)  # the outage consumed no cycle
    met = next(r for r in rows if r.trigger == RevalTrigger.CRITERIA_MET)
    assert met.measurements["n"] == met.measurements["plan_n"]  # evidence kept across the outage
    assert met.measurements["tokens_unverified"] == 3 * 3000  # the wasted attempts: accounted
    assert met.measurements["unverified"] == 3.0 and met.measurements["outage_streak"] == 0.0
    probes = [r for r in rows if r.trigger == RevalTrigger.OUTAGE_PROBE]
    assert probes
    for r in probes:  # each probe persisted what it observed
        assert r.facts["verification_available"] == bool(r.measurements["healthy"])
    again = svc.replay(d)
    assert again.new_transitions == []  # reproduced from the stored rows alone
    assert again.overlay is not None and KEY not in again.overlay.cells  # and released


def test_the_replay_source_reproduces_the_persisted_availability(engine: Engine) -> None:
    d = registered(engine, gated())
    svc, log = services(engine, history(d))
    world = World()
    src = script(d, world)
    svc.sync(d)
    svc.run(d, src)
    world.down = True
    for _ in range(3):
        svc.run(d, src)
    rows = log.transitions(TENANT, "adaptive", 1)
    probe = next(r for r in rows if r.trigger == RevalTrigger.OUTAGE_PROBE)
    assert probe.observed_at is not None
    fleet = svc.replay(d).fleet
    assert fleet is not None
    ref = fleet.monitor.refs[KEY]
    replay_src = ReplaySource(log.trials(TENANT, "adaptive", 1), rows, TENANT)
    assert replay_src.facts(ref, probe.observed_at).verification_available is False
    assert probe.facts["verification_available"] is False  # persisted, not re-probed


# ---- gate failures and refusals ------------------------------------------------------------------
@pytest.mark.parametrize("cycles", [1, 2])
def test_a_failed_gate_is_persisted_with_its_report_and_the_cell_stays_prior_disabled(
    engine: Engine, cycles: int
) -> None:
    d = registered(engine, gated(max_cycles=cycles))
    svc, log = services(engine, history(d))
    svc.sync(d)
    src = script(d, all_prov={"task_family": "pool", "executor_id": "rogue"})
    held = True
    for _ in range(30):
        run = svc.run(d, src)
        held &= run.overlay is not None and KEY in run.overlay.cells
        if triggers(log)[-1] in ("GATE_FAILED", "CYCLES_EXHAUSTED"):
            break
    rows = log.transitions(TENANT, "adaptive", 1)
    assert held and RevalState.RECOVERED not in {r.to_state for r in rows}
    last = rows[-1]  # the statistics were met, the gates were not: the report says which
    assert last.gates["eligible"] is False
    assert last.gates["failed_gates"] == ["executor_compatibility"]
    assert "gate:executor_compatibility" in last.detail
    if cycles == 2:  # the first failed cycle returns the case to quarantine
        assert last.trigger == RevalTrigger.GATE_FAILED and last.to_state == RevalState.QUARANTINED
        assert last.cycle == 0
    else:  # the only cycle is spent: the episode ends, still prior-disabled
        assert last.trigger == RevalTrigger.CYCLES_EXHAUSTED
        assert last.to_state == RevalState.BUDGET_EXHAUSTED
    assert svc.replay(d).new_transitions == []  # the refusal replays exactly too


def test_the_service_and_the_database_each_refuse_an_unauthorised_recovery(engine: Engine) -> None:
    d = registered(engine, gated())
    svc, log = services(engine, history(d))
    svc.sync(d)
    recovered(svc, d, script(d))
    rows = log.transitions(TENANT, "adaptive", 1)
    done = next(r for r in rows if r.to_state == RevalState.RECOVERED)
    fleet = svc.replay(d).fleet
    config = d.params.revalidation.model_dump(mode="json")  # type: ignore[union-attr]
    forgeries = {
        "no report": {},
        "not eligible": {**done.gates, "eligible": False, "failed_gates": ["evidence_freshness"]},
        "no evidence": {**done.gates, "evidence_ids": []},
        "another configuration": {**done.gates, "config_hash": "0" * 64},
        "only a populated payload": {"eligible": "yes", "evidence": [1], "note": "trust me"},
    }
    for name, gates in forgeries.items():
        forged = done.model_copy(update={"seq": done.seq + 1, "gates": gates})
        with pytest.raises(RecoveryGateViolation):
            svc._persist(d, fleet, [forged], [])  # type: ignore[attr-defined]
        with pytest.raises(sa.exc.IntegrityError):
            log.append(TENANT, "adaptive", 1, [forged], [], T0, config)  # the database refuses too
        assert len(log.transitions(TENANT, "adaptive", 1)) == len(rows), name
    ok = done.model_copy(update={"seq": done.seq + 1})  # the genuine report is accepted
    log.append(TENANT, "adaptive", 1, [ok], [], T0, config)
    assert len(log.transitions(TENANT, "adaptive", 1)) == len(rows) + 1


def test_tampered_stored_evidence_is_detected_when_the_log_is_replayed(engine: Engine) -> None:
    from experience_engine.controller.revalidation_service import RevalidationReplayMismatch

    d = registered(engine, gated())
    svc, _ = services(engine, history(d))
    svc.sync(d)
    recovered(svc, d, script(d))
    raw = d.params.model_dump(mode="json")
    raw["revalidation"]["recovery"]["accepted_executors"] = ["someone-else"]
    tampered = d.model_copy(update={"params": ControllerParams.model_validate(raw)})
    with pytest.raises(RevalidationReplayMismatch):
        svc.replay(tampered)  # a different gate policy cannot silently re-explain stored decisions


# ---- atomicity, idempotence, provenance ----------------------------------------------------------
def trial(i: int, **res: Any) -> RevalTrial:
    base: dict[str, Any] = {"verdict": S, "tokens": 7, "task_ref": f"t{i}", "observed_at": T0}
    return RevalTrial(
        key=TrialKey(cell_key=KEY, episode=1, cycle=0, ordinal=i), anchor="a", round_at=T0,
        result=TrialResult(**{**base, **res}),
    )  # fmt: skip


def test_provenance_origin_and_every_outcome_class_round_trip_through_the_database(
    engine: Engine,
) -> None:
    d = registered(engine, gated())
    _, log = services(engine, [])
    prov = TrialProvenance(
        executor_id="e", model_id="m", tool_versions={}, verifier_independent=False,
        verification_status=VerificationStatus.NOT_COMPLETED,
    )  # fmt: skip
    sent = [
        trial(0, provenance=prov),
        trial(1, verdict=TrialVerdict.TIMEOUT, timeout_origin=TimeoutOrigin.CAPABILITY),
        trial(2, verdict=TrialVerdict.INFRASTRUCTURE_FAILURE,
              timeout_origin=TimeoutOrigin.INFRASTRUCTURE),
        trial(3, verdict=TrialVerdict.INFRASTRUCTURE_FAILURE),
        trial(4, verdict=TrialVerdict.EXECUTION_INCOMPLETE, tokens=0),
        trial(5, verdict=U),
        trial(6),  # nothing attested at all
    ]  # fmt: skip
    log.append(TENANT, "adaptive", 1, [], sent, T0, {})
    got = log.trials(TENANT, "adaptive", 1)
    assert [t.model_dump(mode="json") for t in got] == [t.model_dump(mode="json") for t in sent]
    assert got[0].result.provenance.tool_versions == {}  # an empty statement stays empty
    assert got[6].result.provenance == TrialProvenance()  # missing stays missing, never filled in
    assert (
        got[1].result.timeout_origin == TimeoutOrigin.CAPABILITY
        and got[3].result.timeout_origin is None
    )
    assert d.version == 1


def test_a_trial_delivered_twice_is_rejected_and_a_failed_append_stores_nothing(
    engine: Engine,
) -> None:
    registered(engine, gated())
    _, log = services(engine, [])
    log.append(TENANT, "adaptive", 1, [], [trial(0)], T0, {})
    with pytest.raises(sa.exc.IntegrityError):  # the same trial again: never counted twice
        log.append(TENANT, "adaptive", 1, [], [trial(0)], T0, {})
    with pytest.raises(sa.exc.IntegrityError):  # one new, one duplicate: the whole round is refused
        log.append(TENANT, "adaptive", 1, [], [trial(1), trial(0)], T0, {})
    assert [t.key.ordinal for t in log.trials(TENANT, "adaptive", 1)] == [0]


def test_an_interrupted_round_leaves_the_persisted_state_untouched_and_resumable(
    engine: Engine,
) -> None:
    d = registered(engine, gated())
    svc, log = services(engine, history(d))
    svc.sync(d)
    src = script(d)
    svc.run(d, src)
    before = (log.transitions(TENANT, "adaptive", 1), log.trials(TENANT, "adaptive", 1))

    class Crash(AttestedScript):
        def run(self, *a: Any, **k: Any) -> Any:
            if self.calls == 7:
                raise RuntimeError("the worker was killed mid-round")
            return super().run(*a, **k)

    crash = Crash(S, pool=500, tokens=3000, tenant=TENANT, all_prov={"task_family": "pool"},
                  runtime=d.snapshot.prior_sources[0].runtime_version,
                  verifier=d.snapshot.prior_sources[0].verifier_version)  # fmt: skip
    with pytest.raises(RuntimeError):
        svc.run(d, crash)
    assert (log.transitions(TENANT, "adaptive", 1), log.trials(TENANT, "adaptive", 1)) == before
    assert svc.replay(d).new_transitions == []  # still exactly reproducible
    recovered(svc, d, script(d))  # and the next healthy round simply continues


# ---- the orchestrator-backed source --------------------------------------------------------------
class FakeOrchestrator:
    """Stands in for `TaskOrchestrator`: what matters here is how the source classifies a run."""

    def __init__(self, termination: TerminationReason, status: OutcomeStatus, tokens: int = 500,
                 results: tuple[VerificationResult, ...] = ()) -> None:  # fmt: skip
        self.termination, self.status, self.tokens, self.results = (
            termination,
            status,
            tokens,
            results,
        )

    def execute(self, task: Any, session: Any, llm: Any, run_tags: Any = None) -> Any:
        out = SimpleNamespace(
            status=self.status, verification_results=list(self.results), tokens_used=self.tokens,
            verifier_version="v-9", tenant_id=TENANT, execution_time_ms=12,
        )  # fmt: skip
        runtime = SimpleNamespace(termination=self.termination)
        return SimpleNamespace(run_id="run-42", runtime=runtime, outcome=out)


def attested_source(build: Any, infra: Any = lambda: True, attested: bool = True) -> Any:
    from test_controller_e2e import pool_tasks

    from experience_engine.controller.revalidation_runtime import (
        OrchestratorTrialSource,
        RevalidationTask,
        TrialAttestation,
    )

    task = pool_tasks(1)[0].task
    return OrchestratorTrialSource(
        tenant_id=TENANT,
        tasks=[RevalidationTask(task, lambda: None, lambda: None) for _ in range(8)],  # type: ignore[arg-type,return-value]
        build=lambda _c: build(),
        runtime_version="rt-1",
        verifier_version="v-9",
        attestation=TrialAttestation(
            executor_id="exec-1",
            model_id="model-a",
            model_version="1",
            tool_versions={"sql": "2"},
            verifier_id="verifier-x",
            verifier_independent=True,
            evidence_source="revalidation-pool",
            pool_id="pool-1",
            infrastructure_available=infra,
        )
        if attested
        else None,
    )


def run_source(src: Any, i: int = 0) -> TrialResult:
    from experience_engine.controller.drift import references_from_snapshot

    ref = references_from_snapshot(gated().snapshot)[0]
    got = src.run(ref, TrialKey(cell_key=KEY, episode=1, cycle=0, ordinal=i), T0)
    assert got is not None
    return got  # type: ignore[no-any-return]


def ok(passed: bool | None = True) -> tuple[VerificationResult, ...]:
    return (VerificationResult(criterion_kind="x", role="goal", passed=passed, detail="d"),)


def test_the_attested_source_classifies_runs_and_records_provenance() -> None:
    Orc = FakeOrchestrator
    T = TerminationReason
    cases = [
        (Orc(T.FINAL, OutcomeStatus.VERIFIED_SUCCESS, results=ok()), TrialVerdict.SUCCESS, None),
        (Orc(T.FINAL, OutcomeStatus.FAILURE, results=ok(False)), TrialVerdict.FAILURE, None),
        (Orc(T.MAX_STEPS, OutcomeStatus.FAILURE, results=ok(False)), TrialVerdict.TIMEOUT,
         TimeoutOrigin.CAPABILITY),
        (Orc(T.DEADLINE, OutcomeStatus.FAILURE, results=ok(False)), TrialVerdict.TIMEOUT,
         TimeoutOrigin.CAPABILITY),  # a healthy-path deadline is the capability's
        (Orc(T.INVALID_ACTION, OutcomeStatus.FAILURE, results=ok(False)),
         TrialVerdict.INVALID_OUTPUT, None),
        (Orc(T.FINAL, OutcomeStatus.UNKNOWN, results=ok(None)), U, None),
        (Orc(T.LLM_ERROR, OutcomeStatus.FAILURE, tokens=321, results=ok(False)),
         TrialVerdict.INFRASTRUCTURE_FAILURE, None),
    ]  # fmt: skip
    for orc, verdict, origin in cases:
        res = run_source(attested_source(lambda o=orc: o))
        assert (res.verdict, res.timeout_origin) == (verdict, origin), (orc.termination, verdict)
        p = res.provenance
        assert (p.executor_id, p.model_id, p.model_version) == ("exec-1", "model-a", "1")
        assert (p.verifier_id, p.verifier_independent, p.pool_id) == ("verifier-x", True, "pool-1")
        assert p.execution_id == "run-42" and p.evidence_source == "revalidation-pool"
        assert p.task_family is not None and p.risk_level is not None
        assert p.tool_versions == {"sql": "2"} and res.verifier_version == "v-9"
    llm_error = run_source(attested_source(lambda: cases[-1][0]))
    assert llm_error.tokens == 321  # the real resources of a failed run are accounted for
    assert llm_error.provenance.verification_status == VerificationStatus.NOT_COMPLETED
    unavailable = run_source(attested_source(lambda: cases[5][0]))
    assert unavailable.provenance.verification_status == VerificationStatus.UNAVAILABLE


def test_a_deadline_is_infrastructure_only_when_the_infrastructure_was_unhealthy() -> None:
    T = TerminationReason
    orc = FakeOrchestrator(T.DEADLINE, OutcomeStatus.FAILURE, results=ok(False))
    healthy = run_source(attested_source(lambda: orc))
    assert (healthy.verdict, healthy.timeout_origin) == (
        TrialVerdict.TIMEOUT,
        TimeoutOrigin.CAPABILITY,
    )
    probes = iter([True, False])  # healthy before the run, down after it
    down = run_source(attested_source(lambda: orc, infra=lambda: next(probes)))
    assert down.verdict == TrialVerdict.INFRASTRUCTURE_FAILURE
    assert down.timeout_origin == TimeoutOrigin.INFRASTRUCTURE and not down.counted
    other = FakeOrchestrator(T.MAX_TOKENS, OutcomeStatus.FAILURE, results=ok(False))
    still = run_source(attested_source(lambda: other, infra=lambda: False))
    assert (
        still.verdict == TrialVerdict.TIMEOUT
    )  # a step/token limit is the capability's regardless


def test_a_crashed_or_interrupted_run_is_an_attempt_without_evidence() -> None:
    from experience_engine.controller.revalidation_runtime import TrialInterrupted

    class Crashing:
        def execute(self, *_a: Any, **_k: Any) -> Any:
            raise ConnectionError("provider unreachable")

    crashed = run_source(attested_source(Crashing))
    assert crashed.verdict == TrialVerdict.INFRASTRUCTURE_FAILURE and not crashed.counted

    class Cancelled:
        def execute(self, *_a: Any, **_k: Any) -> Any:
            raise TrialInterrupted(tokens=777)

    cut = run_source(attested_source(Cancelled))
    assert cut.verdict == TrialVerdict.EXECUTION_INCOMPLETE and cut.tokens == 777
    assert (
        cut.provenance.verification_status == VerificationStatus.NOT_COMPLETED and not cut.counted
    )


def test_without_an_attestation_the_source_keeps_the_m63_semantics_and_attests_nothing() -> None:
    T = TerminationReason
    orc = FakeOrchestrator(T.LLM_ERROR, OutcomeStatus.FAILURE, results=ok(False))
    res = run_source(attested_source(lambda: orc, attested=False))
    assert res.verdict == TrialVerdict.FAILURE  # M6.3 counted an LLM error as a failure
    assert res.provenance == TrialProvenance() and res.timeout_origin is None

    class Crashing:
        def execute(self, *_a: Any, **_k: Any) -> Any:
            raise ConnectionError("x")

    with pytest.raises(ConnectionError):  # and it does not swallow errors it never classified
        run_source(attested_source(Crashing, attested=False))


def test_the_attested_executor_reports_its_identity_in_the_facts_the_gates_preflight() -> None:
    from experience_engine.controller.drift import references_from_snapshot
    from experience_engine.controller.recovery_gates import preflight
    from experience_engine.domain.recovery import RecoveryPolicy

    d = gated()
    ref = references_from_snapshot(d.snapshot)[0]
    src = attested_source(lambda: FakeOrchestrator(TerminationReason.FINAL, OutcomeStatus.FAILURE))
    facts = src.facts(ref, T0 + timedelta(hours=1))
    assert (facts.executor_id, facts.model_id, facts.verifier_independent) == (
        "exec-1",
        "model-a",
        True,
    )
    verifiers = {"verifier-x": ["v-9"]}
    policy = RecoveryPolicy.model_validate(
        {**RECOVERY, "validated_runtimes": ["rt-1"], "accepted_verifiers": verifiers}
    )
    assert preflight(policy, ref.runtime_version, facts) == []
    plain = attested_source(lambda: None, attested=False).facts(ref, T0)
    assert plain.executor_id is None and plain.infrastructure_available  # nothing invented
