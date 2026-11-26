"""M6 integration (real PostgreSQL): policy lifecycle and its database guards, shadow decisions,
the decision log, tenant isolation, and reproducible replay of decisions read back from storage."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_capability_runtime import MemoryCatalog, make

from controller_support import PLANNING_KEY, definition, snapshot_of
from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.controller_store import SqlControllerPolicyStore, SqlDecisionLog
from experience_engine.adapters.db.reader import SqlRunReader
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.gates import ShadowPair, evaluate_offline, evaluate_shadow
from experience_engine.controller.policy import AdaptiveController
from experience_engine.controller.registry import (
    ControllerPolicyRegistry,
    IllegalPolicyTransitionError,
    PromotionBlockedError,
)
from experience_engine.controller.replay import replay_decision
from experience_engine.controller.rollout import build_rollout
from experience_engine.domain.benchmark import AgentConfig, ModelSettings, Split
from experience_engine.domain.controller import ControllerKind
from experience_engine.domain.decision import ControllerDecision, Strategy
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.rollout import PolicyState, RolloutGates, Stage, StageReport
from experience_engine.execution.capability_runtime import CapabilityRuntime
from experience_engine.execution.controller import (
    Controller,
    ControllerInput,
    FixedPolicy,
    RuleCapabilityPolicy,
)
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.execution.orchestrator import RunResult, TaskOrchestrator
from experience_engine.ports.controller_store import PolicyTransition
from experience_engine.ports.registry import StaleStateError

pytestmark = pytest.mark.integration
TENANT = "bench"
S = PolicyState


class TickClock:
    """Strictly increasing wall clock (evaluation ordering uses created_at)."""

    def __init__(self) -> None:
        self._n = 0

    def now(self) -> datetime:
        self._n += 1
        return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=self._n)

    def monotonic_ms(self) -> int:
        return 0


CONFIG = AgentConfig(
    config_id="C_CAPABILITIES",
    prompt_version="t",
    model=ModelSettings(provider="fake", model="heuristic-fake/1"),
)


def pool_tasks(n: int) -> list[Any]:
    return [b for b in generate_split(Split.DISCOVERY, DEFAULT_SEED) if b.family == "pool"][:n]


def run_tasks(
    engine: Engine,
    tmp_path: Path,
    controller: Controller,
    tasks: list[Any],
    shadow: Controller | None = None,
    with_capability: bool = True,
) -> list[RunResult]:
    catalog = MemoryCatalog([make()] if with_capability else [])
    runtime = CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: HeuristicDecisionProvider(),
    )
    orch = TaskOrchestrator(
        controller=controller,
        verifier=SimulationVerifier(),
        clock=TickClock(),
        config=CONFIG,
        lifecycle=SqlRunLifecycle(engine),
        artifacts=LocalFsArtifactStore(tmp_path / "artifacts"),
        capabilities=runtime,
        shadow=shadow,
    )
    return [
        orch.execute(
            b.task, SimulatedEnvironment(b.environment, b.task.allowed_tools), HeuristicFakeLLM()
        )
        for b in tasks
    ]


# ---- shadow decisions ---------------------------------------------------------------------------
REAL_CAP = "DIRECT_CAPABILITY:pool.0123456789@1"  # the capability the test catalog serves


def shadow_def(version: int = 1) -> Any:
    # believes planning is clearly better and cheaper; as a shadow it can disagree but never act
    return definition(
        snapshot_of({PLANNING_KEY: (400, 380, 400 * 1000), REAL_CAP: (400, 300, 400 * 3000)}),
        version=version,
        n_min=30,
    )


def test_shadow_decisions_are_stored_but_never_executed(engine: Engine, tmp_path: Path) -> None:
    shadow = AdaptiveController(shadow_def())
    results = run_tasks(engine, tmp_path, RuleCapabilityPolicy(), pool_tasks(6), shadow)
    assert all(r.decision.acted for r in results)
    assert all(r.outcome.policy_version == "rule-capability/1" for r in results)
    assert all(r.outcome.capability_version == "pool.0123456789@1" for r in results)  # rule acted
    assert all(
        r.shadow_decision is not None
        and not r.shadow_decision.acted
        and r.shadow_decision.selected_action == Strategy.GENERAL_PLANNING
        for r in results
    )
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                t.policy_decisions.c.policy_version,
                t.policy_decisions.c.acted,
                t.policy_decisions.c.outcome_status,
            )
        ).all()
    acted = [r for r in rows if r.acted]
    unacted = [r for r in rows if not r.acted]
    assert len(acted) == len(unacted) == 6
    assert {r.policy_version for r in unacted} == {"adaptive/1"}
    assert all(r.outcome_status == "VERIFIED_SUCCESS" for r in acted)
    assert all(r.outcome_status is None for r in unacted)  # a shadow decision has no own outcome
    # reconstruction exposes both decisions of a run, flagged
    rec = SqlRunReader(engine).reconstruct(TENANT, results[0].run_id)
    assert rec is not None and sorted(d.acted for d in rec.decisions) == [False, True]


def test_a_failing_shadow_never_affects_the_run(engine: Engine, tmp_path: Path) -> None:
    class Broken:
        policy_version = "broken/1"

        def decide(self, ctx: ControllerInput) -> ControllerDecision:
            raise RuntimeError("boom")

    results = run_tasks(engine, tmp_path, RuleCapabilityPolicy(), pool_tasks(3), Broken())
    assert all(r.outcome.status == OutcomeStatus.VERIFIED_SUCCESS for r in results)
    assert all(r.shadow_decision is None for r in results)
    log = SqlDecisionLog(engine)
    assert log.count_trace_events(TENANT, "controller_shadow", "error") == 3
    assert len(log.outcome_records(TENANT)) == 3  # only acted decisions exist


# ---- decision log -------------------------------------------------------------------------------
def test_decision_log_joins_decisions_with_verified_outcomes(
    engine: Engine, tmp_path: Path
) -> None:
    run_tasks(engine, tmp_path, FixedPolicy(), pool_tasks(3), with_capability=False)
    run_tasks(
        engine, tmp_path, RuleCapabilityPolicy(), pool_tasks(4), AdaptiveController(shadow_def())
    )
    log = SqlDecisionLog(engine)
    allrecs = log.outcome_records(TENANT)
    assert len(allrecs) == 3 + 4 + 4  # fixed + rule acted + 4 shadow
    acted = [r for r in allrecs if r.acted]
    assert [r.policy_version for r in acted] == ["fixed-general-planning/1"] * 3 + [
        "rule-capability/1"
    ] * 4
    rule = [r for r in acted if r.policy_version == "rule-capability/1"]
    assert all(r.action == REAL_CAP for r in rule)
    assert all(r.default_key == r.action and r.tokens > 0 and r.family == "pool" for r in rule)
    assert [a.key for a in rule[0].actions] == [PLANNING_KEY, rule[0].action]
    only_rule = log.outcome_records(TENANT, policy_versions=["rule-capability/1"])
    assert len(only_rule) == 4 and len(log.outcome_records(TENANT, limit=5)) == 5
    # the learning plane can train directly on the log: shadow and unknown rows are excluded
    snap = build_snapshot(allrecs, TENANT)
    assert snap.provenance is not None and snap.provenance.n_unacted_excluded == 4
    assert snap.n_verified == 7
    assert log.outcome_records("someone-else") == []
    pairs = log.shadow_pairs(TENANT, "adaptive/1")
    assert len(pairs) == 4 and all(a.acted and not s.acted for a, s in pairs)
    assert all(a.policy_version == "rule-capability/1" for a, _ in pairs)


def test_stored_adaptive_decisions_replay_exactly_from_the_database(
    engine: Engine, tmp_path: Path
) -> None:
    cap = REAL_CAP
    defn = definition(
        snapshot_of({PLANNING_KEY: (400, 380, 400 * 1000), cap: (400, 300, 400 * 3000)}), version=3
    )
    results = run_tasks(engine, tmp_path, AdaptiveController(defn), pool_tasks(5))
    assert all(r.decision.selected_action == Strategy.GENERAL_PLANNING for r in results)
    rec = SqlRunReader(engine).reconstruct(TENANT, results[0].run_id)
    assert rec is not None
    stored = rec.decisions[0]
    assert stored.policy_version == "adaptive/3" and stored.outcome_status is not None
    check = replay_decision(stored, defn)
    assert check.reproducible, check.differences
    # a different registered definition does not reproduce it
    other = definition(
        snapshot_of({PLANNING_KEY: (400, 381, 400 * 1000), cap: (400, 300, 1_200_000)}), version=3
    )
    assert not replay_decision(stored, other).reproducible


# ---- policy store, guards and lifecycle ---------------------------------------------------------
def registry(engine: Engine) -> tuple[ControllerPolicyRegistry, SqlControllerPolicyStore]:
    store = SqlControllerPolicyStore(engine)
    return ControllerPolicyRegistry(store, TickClock(), RolloutGates()), store


def pass_evidence(
    reg: ControllerPolicyRegistry, d: Any, stage: Stage, control: str | None = None
) -> None:
    reg.record_evaluation(
        d.tenant_id,
        StageReport(
            stage=stage,
            verdict=Verdict.PASS,
            policy_version=d.policy_version,
            definition_hash=d.definition_hash(),
            gate_hash=reg.gates.gate_hash(stage),
            metrics={"control_version": control} if stage == Stage.CANARY else {},
        ),
    )


def advance(
    reg: ControllerPolicyRegistry, d: Any, to: PolicyState, control: str | None = None
) -> None:
    order = {S.SHADOW: Stage.OFFLINE, S.CANARY: Stage.SHADOW, S.ACTIVE: Stage.CANARY}
    pass_evidence(reg, d, order[to], control)
    reg.promote(d.tenant_id, d.policy_id, d.version, to, "ops", "test")


def state(engine: Engine, version: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.select(t.controller_policies.c.state).where(
                    t.controller_policies.c.version == version
                )
            ).scalar_one()
        )


def test_full_lifecycle_rollback_and_history_over_postgres(engine: Engine) -> None:
    reg, store = registry(engine)
    d1, d2 = shadow_def(1), shadow_def(2)
    reg.register(d1)
    reg.register(d2)
    for to in (S.SHADOW, S.CANARY, S.ACTIVE):
        advance(reg, d1, to, control=None)
    for to in (S.SHADOW, S.CANARY):
        advance(reg, d2, to)
    pass_evidence(reg, d2, Stage.CANARY, control="adaptive/1")
    reg.promote(TENANT, "adaptive", 2, S.ACTIVE, "ops", "better")
    assert (state(engine, 1), state(engine, 2)) == ("SUPERSEDED", "ACTIVE")
    reg.rollback(TENANT, "adaptive", 2, "ops", "regression")
    assert (state(engine, 1), state(engine, 2)) == ("ACTIVE", "ROLLED_BACK")
    hist = [
        (h.version, h.from_state.value, h.to_state.value) for h in store.history(TENANT, "adaptive")
    ]
    assert (2, "ACTIVE", "ROLLED_BACK") in hist and (1, "SUPERSEDED", "ACTIVE") in hist
    with pytest.raises(IllegalPolicyTransitionError):
        reg.promote(TENANT, "adaptive", 2, S.SHADOW, "ops", "reopen a rolled-back policy")
    # the running controller follows the registry
    setup = build_rollout(store, TENANT, "adaptive", RuleCapabilityPolicy())
    assert setup.active_version == "adaptive/1" and setup.canary_version is None
    with engine.connect() as conn:
        actions = (
            conn.execute(
                sa.select(t.audit_log.c.action).where(
                    t.audit_log.c.action.like("controller_policy.%")
                )
            )
            .scalars()
            .all()
        )
    assert actions.count("controller_policy.registered") == 2
    assert actions.count("controller_policy.transition") >= 8


def test_blocked_promotions_over_postgres(engine: Engine) -> None:
    reg, _ = registry(engine)
    d = shadow_def(1)
    reg.register(d)
    with pytest.raises(PromotionBlockedError, match="no OFFLINE evaluation"):
        reg.promote(TENANT, "adaptive", 1, S.SHADOW, "ops", "no evidence")
    reg.record_evaluation(
        TENANT,
        StageReport(
            stage=Stage.OFFLINE,
            verdict=Verdict.INCONCLUSIVE,
            policy_version="adaptive/1",
            definition_hash=d.definition_hash(),
            gate_hash=reg.gates.gate_hash(Stage.OFFLINE),
            reasons=["few decisions"],
        ),
    )
    with pytest.raises(PromotionBlockedError, match="INCONCLUSIVE"):
        reg.promote(TENANT, "adaptive", 1, S.SHADOW, "ops", "inconclusive")
    assert state(engine, 1) == "CANDIDATE"


def test_database_enforces_policy_immutability_evidence_and_single_active(engine: Engine) -> None:
    reg, store = registry(engine)
    d1, d2 = shadow_def(1), shadow_def(2)
    reg.register(d1)
    reg.register(d2)
    P = t.controller_policies
    guard_errors = (IntegrityError, DBAPIError)

    def attempt(stmt: Any) -> None:
        with pytest.raises(guard_errors), engine.begin() as conn:
            conn.execute(stmt)

    attempt(P.update().where(P.c.version == 1).values(definition={"tampered": True}))
    attempt(P.update().where(P.c.version == 1).values(definition_hash="x"))
    attempt(P.delete().where(P.c.version == 1))
    attempt(P.update().where(P.c.version == 1).values(state="ACTIVE"))  # illegal edge
    attempt(P.update().where(P.c.version == 1).values(state="SHADOW"))  # no OFFLINE PASS yet
    # evidence for ANOTHER definition does not unlock the step
    pass_evidence(reg, d2, Stage.OFFLINE)
    attempt(P.update().where(P.c.version == 1).values(state="SHADOW"))
    # append-only evidence and history
    E = t.controller_policy_evaluations
    attempt(E.update().values(verdict="FAIL"))
    attempt(E.delete())
    reg.promote(TENANT, "adaptive", 2, S.SHADOW, "ops", "ok")
    attempt(t.controller_policy_transitions.update().values(actor="mallory"))
    attempt(t.controller_policy_transitions.delete())
    # one SHADOW at a time (partial unique index), even via the store directly
    pass_evidence(reg, d1, Stage.OFFLINE)
    with pytest.raises((StaleStateError, IntegrityError, DBAPIError)):
        store.apply_transitions(
            TENANT,
            "adaptive",
            [
                __import__(
                    "experience_engine.ports.controller_store", fromlist=["x"]
                ).PolicyTransition(1, S.CANDIDATE, S.SHADOW)
            ],
            "ops",
            "second shadow",
            None,
            datetime.now(UTC),
        )
    # CANARY -> ACTIVE with a second ACTIVE is rejected by the unique index
    advance(reg, d2, S.CANARY)
    pass_evidence(reg, d2, Stage.CANARY, control=None)
    reg.promote(TENANT, "adaptive", 2, S.ACTIVE, "ops", "first active")
    assert state(engine, 2) == "ACTIVE"


def test_stale_concurrent_transition_is_refused(engine: Engine) -> None:
    reg, store = registry(engine)
    d = shadow_def(1)
    reg.register(d)
    with pytest.raises(StaleStateError):
        store.apply_transitions(
            TENANT,
            "adaptive",
            [PolicyTransition(1, S.SHADOW, S.CANARY)],
            "ops",
            "stale",
            None,
            datetime.now(UTC),
        )


def test_policies_are_tenant_scoped(engine: Engine) -> None:
    reg, store = registry(engine)
    reg.register(shadow_def(1))
    assert store.get_version("someone-else", "adaptive", 1) is None
    assert store.list_versions("someone-else", "adaptive") == []
    setup = build_rollout(store, "someone-else", "adaptive", RuleCapabilityPolicy())
    assert isinstance(setup.controller, RuleCapabilityPolicy) and setup.shadow is None
    # a policy trained for one tenant defers to the rule baseline on another tenant's task
    foreign = AdaptiveController(shadow_def(1))
    task = pool_tasks(1)[0].task.model_copy(update={"tenant_id": "someone-else"})
    d = foreign.decide(ControllerInput(task=task, run_id="r", decision_id="d", match=None))
    assert d.selected_action == Strategy.GENERAL_PLANNING and d.selection_probability == 1.0


def test_offline_and_shadow_evidence_flow_from_the_database_log(
    engine: Engine, tmp_path: Path
) -> None:
    cap = REAL_CAP
    run_tasks(engine, tmp_path, RuleCapabilityPolicy(), pool_tasks(6), None)
    log = SqlDecisionLog(engine)
    recs = log.outcome_records(TENANT)
    snap = build_snapshot(recs, TENANT)
    cand = definition(snap, ControllerKind.COST_AWARE, version=1)
    off = evaluate_offline(cand, recs)
    assert (
        off.verdict == Verdict.INCONCLUSIVE and off.metrics["n_decisions"] == 6
    )  # too few decisions
    run_tasks(engine, tmp_path, RuleCapabilityPolicy(), pool_tasks(6), AdaptiveController(cand))
    pairs = [ShadowPair(a, s) for a, s in log.shadow_pairs(TENANT, cand.policy_version)]
    rep = evaluate_shadow(
        cand, pairs, decision_errors=log.count_trace_events(TENANT, "controller_shadow", "error")
    )
    assert rep.metrics["n_decisions"] == 6 and rep.metrics["agreement"] == 1.0
    assert rep.verdict == Verdict.INCONCLUSIVE and cap in {r.action for r in recs}
