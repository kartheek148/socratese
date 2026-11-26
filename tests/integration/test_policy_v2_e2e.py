"""M5.9 against real PostgreSQL: the candidate policy governing the same lifecycle, historical
immutability, candidate/ACTIVE isolation and rollback, and persistence of the new evidence."""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_memory_governance import built
from test_memory_governance_e2e import MID, TENANT, active_ids, state_of
from test_policy_v2 import big_plan, good_arms
from test_registry_e2e import LOW

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.memory_registry_store import SqlMemoryRegistryStore
from experience_engine.adapters.db.memory_store import SqlMemoryStore
from experience_engine.domain.capability import CapabilityState as S
from experience_engine.domain.evaluation import (
    Verdict,
    default_policies,
    default_policies_v2,
)
from experience_engine.evaluation.engine import evaluate as run_evaluation
from experience_engine.governance.memory import MemoryRegistryService
from experience_engine.governance.registry import IllegalTransitionError
from experience_engine.memory.service import register_candidates
from reports import make_report
from support import FakeClock

pytestmark = pytest.mark.integration
V2 = default_policies_v2()


@pytest.fixture
def world(engine: Engine) -> tuple[MemoryRegistryService, Engine]:
    register_candidates(SqlMemoryStore(engine), built())
    svc = MemoryRegistryService(SqlMemoryRegistryStore(engine), V2, FakeClock())
    return svc, engine


def report(verdict: Verdict, *, mid: str = MID, version: int = 1, policy: Any = None) -> Any:
    pol = policy or V2["low"]
    return make_report(
        capability_id=mid, version=version, tenant=TENANT, verdict=verdict, policy=pol
    ).model_copy(update={"asset_kind": "MEMORY"})


def evaluate_with(svc: MemoryRegistryService, verdict: Verdict, **kw: Any) -> S:
    mid = kw.get("mid", MID)
    rec = svc._get(TENANT, mid, kw.get("version", 1))  # noqa: SLF001
    if rec.state == S.CANDIDATE:
        svc.start_validation(TENANT, mid, kw.get("version", 1), "ev")
    return svc.complete_evaluation(report(verdict, **kw), "ev")


def promote(svc: MemoryRegistryService, engine: Engine, mid: str = MID) -> None:
    store = SqlMemoryRegistryStore(engine)
    svc.start_shadow(TENANT, mid, 1, "ev")
    for mode in ("SHADOW", "CANARY"):
        for i in range(V2["low"].min_shadow_observations):
            svc.record_observation(
                store.get_version(TENANT, mid, 1), mode, f"{mode}{i}", True, True, 5, 9
            )  # type: ignore[arg-type]
        if mode == "SHADOW":
            svc.advance_to_canary(TENANT, mid, 1, "ev")
    svc.activate(TENANT, mid, 1, "ev")


def test_v2_governs_the_same_lifecycle_and_only_pass_reaches_approval(world: Any) -> None:
    svc, engine = world
    assert evaluate_with(svc, Verdict.INCONCLUSIVE) == S.VALIDATING  # more evidence: not APPROVED
    assert state_of(engine) == "VALIDATING" and active_ids(engine) == []
    assert evaluate_with(svc, Verdict.PASS) == S.APPROVED
    promote(svc, engine)
    assert state_of(engine) == "ACTIVE" and active_ids(engine) == [f"{MID}@1"]
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(t.memory_evaluations.c.policy_name, t.memory_evaluations.c.policy_hash)
        ).all()
    assert {r[0] for r in rows} == {"default-low-v2"}
    assert {r[1] for r in rows} == {V2["low"].policy_hash()}


def test_candidate_and_rejected_assets_stay_unreachable_while_another_is_active(world: Any) -> None:
    svc, engine = world
    evaluate_with(svc, Verdict.PASS)
    promote(svc, engine)
    other = "mem_episodic_pool"
    evaluate_with(svc, Verdict.FAIL, mid=other)
    assert state_of(engine, mid=other) == "REJECTED"
    others = [m for m in ("mem_procedural_pool", "mem_consolidated_timeout") if m != MID]
    for m in others:
        assert state_of(engine, mid=m) == "CANDIDATE"
    assert active_ids(engine) == [f"{MID}@1"]  # only the evaluated-and-promoted one


def test_rollback_works_under_the_candidate_policy(world: Any) -> None:
    svc, engine = world
    evaluate_with(svc, Verdict.PASS)
    promote(svc, engine)
    svc.rollback(TENANT, MID, 1, "ev", "post-run drill")
    assert state_of(engine) == "ROLLED_BACK" and active_ids(engine) == []


def test_a_new_policy_never_reopens_or_rewrites_a_rejected_version(engine: Engine) -> None:
    register_candidates(SqlMemoryStore(engine), built())
    v1_svc = MemoryRegistryService(SqlMemoryRegistryStore(engine), default_policies(), FakeClock())
    evaluate_with(v1_svc, Verdict.FAIL, policy=LOW)
    assert state_of(engine) == "REJECTED"
    ev = t.memory_evaluations
    with engine.connect() as conn:
        before = conn.execute(sa.select(ev).order_by(ev.c.evaluation_id)).all()
    v2_svc = MemoryRegistryService(SqlMemoryRegistryStore(engine), V2, FakeClock())
    with pytest.raises(IllegalTransitionError):  # REJECTED is terminal under any policy
        v2_svc.complete_evaluation(report(Verdict.PASS), "ev")
    with pytest.raises(IllegalTransitionError):
        v2_svc.start_validation(TENANT, MID, 1, "ev")
    with engine.connect() as conn:
        after = conn.execute(sa.select(ev).order_by(ev.c.evaluation_id)).all()
    assert before == after and len(before) == 1 and before[0].policy_name == "default-low"
    with pytest.raises((DBAPIError, IntegrityError)), engine.begin() as conn:  # append-only
        conn.execute(sa.text("UPDATE memory_evaluations SET verdict='PASS'"))
    assert active_ids(engine) == []


def test_new_evidence_is_persisted_with_the_evaluation(world: Any) -> None:
    svc, engine = world
    arms = good_arms()
    rep = run_evaluation(
        big_plan(arms, policy=V2["low"], counterfactual_policies=[default_policies()["low"]])
    ).model_copy(update={"capability_id": MID, "asset_kind": "MEMORY", "tenant_id": TENANT})
    svc.start_validation(TENANT, MID, 1, "ev")
    svc.complete_evaluation(rep, "ev")
    with engine.connect() as conn:
        (metrics,) = conn.execute(
            sa.select(t.memory_evaluations.c.metrics).where(t.memory_evaluations.c.memory_id == MID)
        ).one()
    assert metrics["evaluation_config_hash"] == rep.evaluation_config_hash
    assert metrics["counterfactuals"][0]["policy_name"] == "default-low"
    assert metrics["risk_evidence"]["n_pairs"] == rep.risk_evidence.n_pairs  # type: ignore[union-attr]
    assert metrics["resources"]["by_arm"]["A_BASELINE"]["runs_executed"] > 0
    assert metrics["early_stopped"] is False
