"""M4 integration tests: evaluator + registry against real PostgreSQL."""

import json
import math
import re
import threading
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_learning_e2e import Env, count

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.registry_store import SqlRegistryStore
from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.bench.dataset import (
    DatasetLoader,
    EvaluatorAccess,
    write_private_dataset,
)
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.registry import (
    PromotionBlockedError,
    RegistryService,
)
from experience_engine.domain.benchmark import AgentConfig, ModelSettings, Split
from experience_engine.domain.capability import CapabilityState as S
from experience_engine.domain.evaluation import Arm, Verdict, default_policies
from experience_engine.evaluation.arms import make_agent_arm, make_capability_arm
from experience_engine.evaluation.engine import EvaluationPlan, evaluate
from experience_engine.ports.decisions import DecisionError, DecisionResult
from experience_engine.ports.registry import StaleStateError, TransitionSpec
from reports import make_report
from support import FakeClock

pytestmark = pytest.mark.integration
COMMITTED = Path(__file__).resolve().parents[2] / "benchmarks" / "v1"
TENANT = "bench"
LOW = default_policies()["low"]


class Oracle:
    """Test-only provider that understands every layout (including unseen transfer variants)."""

    def decide(self, *, template_id, objective, inputs, output_schema, max_tokens):  # type: ignore[no-untyped-def]
        try:
            return HeuristicDecisionProvider().decide(
                template_id=template_id,
                objective=objective,
                inputs=inputs,
                output_schema=output_schema,
                max_tokens=max_tokens,
            )
        except DecisionError:
            pass
        cfg = json.loads(inputs["config_text"])
        if "spring" in cfg:
            demand = cfg["server"]["tomcat"]["threads"]["max"]
            return DecisionResult(
                {"key": "spring.datasource.hikari.maximumPoolSize", "value": demand}, 60
            )
        if "okhttp" in cfg:
            worst = max(int(x) for x in re.findall(r"latency_ms=(\d+)", inputs["log_text"]))
            return DecisionResult(
                {"key": "okhttp.readTimeoutMillis", "value": math.ceil(worst * 1.2 / 100) * 100}, 60
            )
        raise DecisionError("unknown layout")


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    write_private_dataset(root, seed=987654321)
    return root


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Env:
    return Env(engine, tmp_path)


def learned(env: Env) -> dict[str, str]:
    env.persist(Split.DISCOVERY, tenant=TENANT)
    env.deliver_events()
    env.process_jobs()
    env.compiler().compile(TENANT)
    return {d.id.split(".")[0]: d.id for d, _ in env.caps.list_versions(TENANT)}


def evaluate_version(
    env: Env, private_root: Path, cap_id: str, version: int, decisions, svc: RegistryService
):  # type: ignore[no-untyped-def]
    store = SqlRegistryStore(env.engine)
    rec = store.get_version(TENANT, cap_id, version)
    assert rec is not None
    if rec.state == S.CANDIDATE:
        svc.start_validation(TENANT, cap_id, version, "ev")
    loader = DatasetLoader(COMMITTED, EvaluatorAccess(), private_root=private_root)
    disc, val, hid = (
        loader.load(s) for s in (Split.DISCOVERY, Split.VALIDATION, Split.HIDDEN_TRANSFER)
    )
    verifier, clock = SimulationVerifier(), FakeClock()
    llm = HeuristicFakeLLM()
    model = ModelSettings(provider="fake", model="heuristic-fake/1")
    cfg = AgentConfig(prompt_version="t", model=model)
    plan = EvaluationPlan(
        tenant_id=TENANT,
        definition=rec.definition,
        policy=svc.policy_for(rec),
        clock=clock,
        arms={
            Arm.BASELINE: make_agent_arm(llm=llm, config=cfg, verifier=verifier, clock=clock),
            Arm.MEMORY: make_agent_arm(
                llm=llm,
                config=cfg,
                verifier=verifier,
                clock=clock,
                hint_for=lambda _t: (
                    "it was solved by: list_files, read_file(config), set_json_value(config)."
                ),
            ),
            Arm.CAPABILITY: make_capability_arm(
                definition=rec.definition,
                tenant_id=TENANT,
                decisions=decisions,
                verifier=verifier,
                clock=clock,
            ),
        },
        source_tasks=disc.tasks,
        heldout_tasks=val.tasks,
        transfer_tasks=hid.tasks,
        other_family_tasks=val.tasks,
        datasets={
            "DISCOVERY": disc.version,
            "VALIDATION": val.version,
            "HIDDEN_TRANSFER": hid.version,
        },
        verifier_version=verifier.version,
        model_settings=model.model_dump(mode="json"),
        seeds=[0],
    )
    report = evaluate(plan)
    svc.complete_evaluation(report, "ev")
    return report


def service(env: Env) -> RegistryService:
    return RegistryService(SqlRegistryStore(env.engine), default_policies(), env.clock)


def states(env: Env) -> dict[tuple[str, int], str]:
    with env.engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                t.capability_versions.c.capability_id,
                t.capability_versions.c.version,
                t.capability_versions.c.state,
            )
        ).all()
    return {(c, v): s for c, v, s in rows}


# ---- evaluation ------------------------------------------------------------------------------
def test_candidate_that_fails_transfer_is_rejected_and_never_executable(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    svc = service(env)
    report = evaluate_version(env, private_root, caps["pool"], 1, HeuristicDecisionProvider(), svc)
    assert report.verdict == Verdict.FAIL
    assert any("transfer success" in r for r in report.reasons)
    transfer = report.suites["TRANSFER"]
    assert transfer["C_CAPABILITY"].verified == 0 and transfer["A_BASELINE"].verified == 0
    assert report.suites["SOURCE_REPLAY"]["C_CAPABILITY"].rate == 1.0  # replay alone looked perfect
    assert states(env)[(caps["pool"], 1)] == "REJECTED"
    assert env.caps.list_executable(TENANT) == []
    with env.engine.connect() as conn:
        ev = conn.execute(sa.select(t.capability_evaluations)).mappings().one()
    assert (
        ev["verdict"] == "FAIL"
        and ev["transfer_n"] == 4
        and ev["heldout_n"] == 4
        and ev["negative_n"] >= 3
    )
    assert ev["policy_hash"] == LOW.policy_hash() and ev["dataset_versions"]["HIDDEN_TRANSFER"][
        "version"
    ].endswith("-private")


def test_capable_candidate_passes_with_full_evidence_and_cost_accounting(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    svc = service(env)
    report = evaluate_version(env, private_root, caps["pool"], 1, Oracle(), svc)
    assert report.verdict == Verdict.PASS, report.reasons
    assert states(env)[(caps["pool"], 1)] == "APPROVED"
    for suite in ("SOURCE_REPLAY", "HELD_OUT", "TRANSFER", "NEGATIVE", "REGRESSION"):
        assert suite in report.suites
    assert report.false_applicability_rate == 0.0 and report.regressions == 0
    neg = report.suites["NEGATIVE"]["C_CAPABILITY"]
    assert neg.n >= 3 and neg.verified == neg.n  # every negative case was refused
    # transfer: capability solves unseen variants that the baseline and memory control cannot
    tr = report.suites["TRANSFER"]
    assert (
        tr["C_CAPABILITY"].rate == 1.0
        and tr["A_BASELINE"].rate == 0.0
        and tr["B_MEMORY"].rate == 0.0
    )
    lc = report.lifecycle
    assert lc.evaluation_overhead_tokens > 0 and lc.learning_wall_ms >= 0
    assert (
        lc.lifecycle_tokens_per_success is not None and lc.baseline_tokens_per_success is not None
    )
    # evaluation runs never become task runs or learning data
    assert count(env.engine, t.task_runs) == 12 and count(env.engine, t.experience_cases) == 12


def test_inconclusive_when_evidence_is_too_small_and_high_risk_cannot_pass_on_this_benchmark(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    store = SqlRegistryStore(env.engine)
    # force the strictest class by evaluating under the high policy
    svc = RegistryService(
        store,
        {
            **default_policies(),
            "low": default_policies()["high"].model_copy(update={"risk_class": "low"}),
        },
        env.clock,
    )
    report = evaluate_version(env, private_root, caps["pool"], 1, Oracle(), svc)
    assert report.verdict == Verdict.INCONCLUSIVE
    assert states(env)[(caps["pool"], 1)] == "VALIDATING"  # waits for more evidence
    assert (
        count(
            env.engine,
            t.capability_evaluations,
            t.capability_evaluations.c.verdict == "INCONCLUSIVE",
        )
        == 1
    )


# ---- lifecycle, rollback, isolation ----------------------------------------------------------
def promote_to_active(env: Env, svc: RegistryService, cap: str, version: int) -> None:
    store = SqlRegistryStore(env.engine)
    rec = store.get_version(TENANT, cap, version)
    assert rec is not None
    svc.start_shadow(TENANT, cap, version, "ev")
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, cap, version), "SHADOW", f"s{i}", True, True, 50, 500
        )  # type: ignore[arg-type]
    svc.advance_to_canary(TENANT, cap, version, "ev")
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, cap, version), "CANARY", f"c{i}", True, True, 50, 500
        )  # type: ignore[arg-type]
    svc.activate(TENANT, cap, version, "ev")


def test_full_lifecycle_new_version_activation_and_rollback_restores_previous(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    cap = caps["pool"]
    svc = service(env)
    assert evaluate_version(env, private_root, cap, 1, Oracle(), svc).verdict == Verdict.PASS
    promote_to_active(env, svc, cap, 1)
    assert states(env)[(cap, 1)] == "ACTIVE"
    assert [d.version for d in env.caps.list_executable(TENANT)] == [1]

    # a new compiler version produces v2; it must earn promotion on its own evidence
    env.compiler(compiler_version="rule-compiler/3").compile(TENANT)
    assert states(env)[(cap, 2)] == "CANDIDATE"
    assert [d.version for d in env.caps.list_executable(TENANT)] == [
        1
    ]  # v2 candidate is not executable
    assert evaluate_version(env, private_root, cap, 2, Oracle(), svc).verdict == Verdict.PASS
    promote_to_active(env, svc, cap, 2)
    st = states(env)
    assert (st[(cap, 1)], st[(cap, 2)]) == ("DEPRECATED", "ACTIVE")
    assert [d.version for d in env.caps.list_executable(TENANT)] == [2]

    svc.rollback(TENANT, cap, 2, "ev", "canary regression in production")
    st = states(env)
    assert (st[(cap, 1)], st[(cap, 2)]) == ("ACTIVE", "ROLLED_BACK")  # previous active never lost
    assert [d.version for d in env.caps.list_executable(TENANT)] == [1]
    pointer = SqlRegistryStore(env.engine).get_pointer(TENANT, cap)
    assert (pointer.active_version, pointer.previous_version) == (1, None)

    with env.engine.connect() as conn:
        trail = conn.execute(
            sa.select(
                t.capability_transitions.c.version, t.capability_transitions.c.to_state
            ).order_by(
                t.capability_transitions.c.created_at, t.capability_transitions.c.transition_id
            )
        ).all()
        audit = conn.execute(
            sa.select(sa.func.count())
            .select_from(t.audit_log)
            .where(t.audit_log.c.action == "capability.transition")
        ).scalar_one()
    assert audit == len(trail) >= 12
    assert (1, "ACTIVE") in trail and (2, "ROLLED_BACK") in trail


def test_promotion_is_blocked_without_rollout_evidence(env: Env, private_root: Path) -> None:
    caps = learned(env)
    svc = service(env)
    evaluate_version(env, private_root, caps["pool"], 1, Oracle(), svc)
    svc.start_shadow(TENANT, caps["pool"], 1, "ev")
    with pytest.raises(PromotionBlockedError, match="shadow observations"):
        svc.advance_to_canary(TENANT, caps["pool"], 1, "ev")


def test_database_enforces_the_lifecycle_even_for_direct_sql(env: Env, private_root: Path) -> None:
    caps = learned(env)
    cap = caps["pool"]

    def set_state(state: str) -> None:
        with env.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE capability_versions SET state = :s "
                    "WHERE capability_id = :c AND version = 1"
                ),
                {"s": state, "c": cap},
            )

    with pytest.raises(DBAPIError, match="illegal capability state transition"):
        set_state("ACTIVE")  # CANDIDATE -> ACTIVE
    with pytest.raises(DBAPIError, match="illegal capability state transition"):
        set_state("SHADOW")
    set_state("VALIDATING")
    with pytest.raises(DBAPIError, match="without a passing evaluation"):
        set_state("APPROVED")  # no PASS evaluation recorded
    store = SqlRegistryStore(env.engine)
    store.record_evaluation(
        make_report(capability_id=cap, version=1, tenant=TENANT, verdict=Verdict.FAIL, policy=LOW),
        "x",
        env.clock.now(),
    )
    with pytest.raises(DBAPIError, match="without a passing evaluation"):
        set_state("APPROVED")  # a FAIL evaluation does not count
    store.record_evaluation(
        make_report(capability_id=cap, version=1, tenant=TENANT, verdict=Verdict.PASS, policy=LOW),
        "y",
        env.clock.now(),
    )
    set_state("APPROVED")


def test_source_replay_only_evidence_can_never_be_recorded_as_pass(env: Env) -> None:
    caps = learned(env)
    store = SqlRegistryStore(env.engine)
    bad = make_report(
        capability_id=caps["pool"],
        version=1,
        tenant=TENANT,
        verdict=Verdict.PASS,
        policy=LOW,
        transfer_n=0,
    )
    with pytest.raises(IntegrityError, match="pass_requires_heldout"):
        store.record_evaluation(bad, "z", env.clock.now())


def test_registry_history_is_append_only_and_one_active_per_capability(env: Env) -> None:
    caps = learned(env)
    cap = caps["pool"]
    svc = service(env)
    store = SqlRegistryStore(env.engine)
    env.compiler(compiler_version="rule-compiler/3").compile(TENANT)
    for v in (1, 2):
        svc.start_validation(TENANT, cap, v, "ev")
        svc.complete_evaluation(
            make_report(
                capability_id=cap, version=v, tenant=TENANT, verdict=Verdict.PASS, policy=LOW
            ),
            "ev",
        )
    for stmt in (
        "UPDATE capability_evaluations SET verdict = 'PASS'",
        "DELETE FROM capability_evaluations",
        "UPDATE capability_transitions SET actor = 'x'",
        "DELETE FROM capability_transitions",
    ):
        with pytest.raises(DBAPIError, match="append-only"), env.engine.begin() as conn:
            conn.execute(sa.text(stmt))
    # walk v1 to ACTIVE, then try to make v2 ACTIVE without leaving v1: the index forbids two ACTIVE
    promote_to_active(env, svc, cap, 1)
    svc.start_shadow(TENANT, cap, 2, "ev")
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, cap, 2), "SHADOW", f"s{i}", True, True, 1, 1
        )  # type: ignore[arg-type]
    svc.advance_to_canary(TENANT, cap, 2, "ev")
    with pytest.raises(IntegrityError), env.engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE capability_versions SET state='ACTIVE' WHERE capability_id=:c AND version=2"
            ),
            {"c": cap},
        )


def test_stale_state_changes_are_rejected_and_roll_back_atomically(env: Env) -> None:
    caps = learned(env)
    cap = caps["pool"]
    store = SqlRegistryStore(env.engine)
    now = env.clock.now()
    with pytest.raises(StaleStateError):
        store.apply_transitions(
            tenant_id=TENANT,
            capability_id=cap,
            transitions=[
                TransitionSpec(1, S.CANDIDATE, S.VALIDATING),
                TransitionSpec(1, S.APPROVED, S.SHADOW),  # wrong expectation for the 2nd step
            ],
            pointer=None,
            actor="ev",
            reason="x",
            evaluation_id=None,
            policy_hash=None,
            now=now,
        )
    assert states(env)[(cap, 1)] == "CANDIDATE"  # the first transition did not stick
    assert (
        count(env.engine, t.capability_transitions) == 0
        and count(env.engine, t.audit_log, t.audit_log.c.action == "capability.transition") == 0
    )


def test_concurrent_activations_leave_exactly_one_active(env: Env) -> None:
    caps = learned(env)
    cap = caps["pool"]
    svc = service(env)
    store = SqlRegistryStore(env.engine)
    env.compiler(compiler_version="rule-compiler/3").compile(TENANT)
    for v in (1, 2):
        svc.start_validation(TENANT, cap, v, "ev")
        svc.complete_evaluation(
            make_report(
                capability_id=cap, version=v, tenant=TENANT, verdict=Verdict.PASS, policy=LOW
            ),
            "ev",
        )
        svc.start_shadow(TENANT, cap, v, "ev")
        for i in range(5):
            svc.record_observation(
                store.get_version(TENANT, cap, v), "SHADOW", f"s{i}", True, True, 1, 1
            )  # type: ignore[arg-type]
        svc.advance_to_canary(TENANT, cap, v, "ev")
        for i in range(5):
            svc.record_observation(
                store.get_version(TENANT, cap, v), "CANARY", f"c{i}", True, True, 1, 1
            )  # type: ignore[arg-type]
    results: list[str] = []
    barrier = threading.Barrier(2)

    def activate(v: int) -> None:
        s = service(env)
        barrier.wait()
        try:
            s.activate(TENANT, cap, v, "ev")
            results.append("ok")
        except Exception as exc:  # the loser may fail on the unique index or a stale pointer
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=activate, args=(v,)) for v in (1, 2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    active = [k for k, s in states(env).items() if s == "ACTIVE"]
    assert len(active) == 1, (results, states(env))
    assert len(env.caps.list_executable(TENANT)) == 1


def test_registry_is_tenant_scoped(env: Env) -> None:
    caps = learned(env)
    cap = caps["pool"]
    store = SqlRegistryStore(env.engine)
    assert store.get_version("other-tenant", cap, 1) is None
    with pytest.raises(StaleStateError):
        store.apply_transitions(
            tenant_id="other-tenant",
            capability_id=cap,
            transitions=[TransitionSpec(1, S.CANDIDATE, S.VALIDATING)],
            pointer=None,
            actor="ev",
            reason="x",
            evaluation_id=None,
            policy_hash=None,
            now=env.clock.now(),
        )
    assert states(env)[(cap, 1)] == "CANDIDATE"
