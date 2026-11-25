"""Memory lifecycle against real PostgreSQL: promotion, rollback, DB-enforced rules, tenancy and
the guarantee that production-mode retrieval can only ever see ACTIVE versions."""

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_memory_governance import NOW, built, experience, facts, pool_memory
from test_registry_e2e import LOW

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.memory_catalog import SqlActiveMemoryCatalog
from experience_engine.adapters.db.memory_registry_store import SqlMemoryRegistryStore
from experience_engine.adapters.db.memory_store import SqlMemoryStore
from experience_engine.domain.capability import CapabilityState as S
from experience_engine.domain.evaluation import Verdict, default_policies
from experience_engine.domain.memory import MemoryType
from experience_engine.governance.memory import MemoryRegistryService
from experience_engine.governance.registry import IllegalTransitionError, PromotionBlockedError
from experience_engine.memory.runtime import select_memory
from experience_engine.memory.service import register_candidates
from experience_engine.ports.registry import StaleStateError
from reports import make_report
from support import FakeClock, first_task

pytestmark = pytest.mark.integration
TENANT = "bench"
MID = "mem_consolidated_pool"


@pytest.fixture
def world(engine: Engine) -> tuple[SqlMemoryStore, MemoryRegistryService, Engine]:
    store = SqlMemoryStore(engine)
    register_candidates(store, built())
    svc = MemoryRegistryService(SqlMemoryRegistryStore(engine), default_policies(), FakeClock())
    return store, svc, engine


def state_of(engine: Engine, version: int = 1, mid: str = MID) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.select(t.memory_versions.c.state).where(
                    t.memory_versions.c.memory_id == mid, t.memory_versions.c.version == version
                )
            ).scalar_one()
        )


def evaluate(svc: MemoryRegistryService, verdict: Verdict, version: int = 1, mid: str = MID,
             tenant: str = TENANT) -> None:  # fmt: skip
    rec = svc._get(tenant, mid, version)  # noqa: SLF001
    if rec.state == S.CANDIDATE:
        svc.start_validation(tenant, mid, version, "ev")
    report = make_report(
        capability_id=mid, version=version, tenant=tenant, verdict=verdict, policy=LOW
    ).model_copy(update={"asset_kind": "MEMORY"})
    svc.complete_evaluation(report, "ev")


def promote(svc: MemoryRegistryService, engine: Engine, version: int = 1, mid: str = MID) -> None:
    store = SqlMemoryRegistryStore(engine)
    svc.start_shadow(TENANT, mid, version, "ev")
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, mid, version), "SHADOW", f"s{i}", True, True, 5, 9
        )  # type: ignore[arg-type]  # noqa: E501
    svc.advance_to_canary(TENANT, mid, version, "ev")
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, mid, version), "CANARY", f"c{i}", True, True, 5, 9
        )  # type: ignore[arg-type]  # noqa: E501
    svc.activate(TENANT, mid, version, "ev")


def active_ids(engine: Engine, tenant: str = TENANT) -> list[str]:
    return [
        f"{d.memory_id}@{d.version}" for d in SqlActiveMemoryCatalog(engine).list_active(tenant)
    ]


def test_candidates_are_registered_as_candidate_and_invisible_to_production(world: Any) -> None:
    store, _svc, engine = world
    assert {s for _, s in store.list_versions(TENANT)} == {S.CANDIDATE}
    assert len(store.list_versions(TENANT)) == 5
    assert active_ids(engine) == []
    sel = select_memory(
        SqlActiveMemoryCatalog(engine), TENANT, MemoryType.CONSOLIDATED, facts(first_task())
    )
    assert sel.artifact is None and "no ACTIVE" in (sel.fallback_reason or "")


def test_only_active_versions_are_retrievable_at_every_lifecycle_stage(world: Any) -> None:
    _store, svc, engine = world
    cat = SqlActiveMemoryCatalog(engine)
    f = facts(first_task())

    def visible() -> bool:
        return select_memory(cat, TENANT, MemoryType.CONSOLIDATED, f).artifact is not None

    assert not visible()
    svc.start_validation(TENANT, MID, 1, "ev")
    assert state_of(engine) == "VALIDATING" and not visible()
    evaluate(svc, Verdict.PASS)
    assert state_of(engine) == "APPROVED" and not visible()
    store = SqlMemoryRegistryStore(engine)
    svc.start_shadow(TENANT, MID, 1, "ev")
    assert not visible()
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, MID, 1), "SHADOW", f"s{i}", True, True, 5, 9
        )  # type: ignore[arg-type]  # noqa: E501
    svc.advance_to_canary(TENANT, MID, 1, "ev")
    assert state_of(engine) == "CANARY" and not visible()
    for i in range(5):
        svc.record_observation(
            store.get_version(TENANT, MID, 1), "CANARY", f"c{i}", True, True, 5, 9
        )  # type: ignore[arg-type]  # noqa: E501
    svc.activate(TENANT, MID, 1, "ev")
    assert state_of(engine) == "ACTIVE" and visible()
    assert active_ids(engine) == [f"{MID}@1"]


def test_a_rejected_memory_is_never_retrievable_and_cannot_be_promoted(world: Any) -> None:
    _store, svc, engine = world
    evaluate(svc, Verdict.FAIL)
    assert state_of(engine) == "REJECTED" and active_ids(engine) == []
    with pytest.raises(IllegalTransitionError):
        svc.start_shadow(TENANT, MID, 1, "ev")
    experimental = SqlActiveMemoryCatalog(engine, {S.REJECTED})
    assert experimental.experimental and len(experimental.list_active(TENANT)) == 1
    assert not SqlActiveMemoryCatalog(engine).experimental


def test_promotion_requires_a_passing_evaluation_and_observations(world: Any) -> None:
    _store, svc, _engine = world
    svc.start_validation(TENANT, MID, 1, "ev")
    with pytest.raises(IllegalTransitionError):
        svc.start_shadow(TENANT, MID, 1, "ev")  # still VALIDATING
    evaluate(svc, Verdict.PASS)
    svc.start_shadow(TENANT, MID, 1, "ev")
    with pytest.raises(PromotionBlockedError, match="shadow observations"):
        svc.advance_to_canary(TENANT, MID, 1, "ev")


def test_rollback_restores_the_previous_active_version_atomically(world: Any) -> None:
    store, svc, engine = world
    evaluate(svc, Verdict.PASS)
    promote(svc, engine)
    c = pool_memory()
    v2 = c.model_copy(update={"content": c.content + " (revised)", "content_hash": "c" * 64})
    assert store.register(v2.model_copy(update={"version": 2}))
    evaluate(svc, Verdict.PASS, version=2)
    promote(svc, engine, version=2)
    assert (state_of(engine, 1), state_of(engine, 2)) == ("DEPRECATED", "ACTIVE")
    assert active_ids(engine) == [f"{MID}@2"]
    svc.rollback(TENANT, MID, 2, "ev", "regression in canary")
    assert (state_of(engine, 1), state_of(engine, 2)) == ("ACTIVE", "ROLLED_BACK")
    assert active_ids(engine) == [f"{MID}@1"]  # production serves the previous version again
    with engine.connect() as conn:
        trail = conn.execute(
            sa.select(t.memory_transitions.c.version, t.memory_transitions.c.to_state)
        ).all()
    assert (2, "ROLLED_BACK") in [tuple(r) for r in trail] and (1, "ACTIVE") in [
        tuple(r) for r in trail
    ]


def test_database_enforces_the_lifecycle_even_for_direct_sql(world: Any) -> None:
    _store, svc, engine = world

    def update(sql: str) -> None:
        with engine.begin() as conn:
            conn.execute(sa.text(sql))

    base = "UPDATE memory_versions SET {} WHERE memory_id='mem_consolidated_pool' AND version=1"
    with pytest.raises((DBAPIError, IntegrityError), match="illegal memory state transition"):
        update(base.format("state='ACTIVE'"))  # CANDIDATE -> ACTIVE
    svc.start_validation(TENANT, MID, 1, "ev")  # writes a transition row (append-only evidence)
    with pytest.raises((DBAPIError, IntegrityError), match="without a passing evaluation"):
        update(base.format("state='APPROVED'"))
    with pytest.raises((DBAPIError, IntegrityError), match="immutable except for state"):
        update(base.format("content_hash='forged'"))
    with pytest.raises((DBAPIError, IntegrityError), match="cannot be deleted"):
        update("DELETE FROM memory_versions")
    with pytest.raises((DBAPIError, IntegrityError)):  # append-only evidence
        update("UPDATE memory_transitions SET reason='x'")


def test_at_most_one_active_version_per_memory(world: Any) -> None:
    store, svc, engine = world
    evaluate(svc, Verdict.PASS)
    promote(svc, engine)
    c = pool_memory()
    store.register(c.model_copy(update={"version": 2, "content_hash": "d" * 64}))
    evaluate(svc, Verdict.PASS, version=2)
    reg = SqlMemoryRegistryStore(engine)
    svc.start_shadow(TENANT, MID, 2, "ev")
    for i in range(5):
        svc.record_observation(reg.get_version(TENANT, MID, 2), "SHADOW", f"s{i}", True, True, 5, 9)  # type: ignore[arg-type]  # noqa: E501
    svc.advance_to_canary(TENANT, MID, 2, "ev")
    stmt = sa.text("UPDATE memory_versions SET state='ACTIVE' WHERE memory_id=:m AND version=2")
    with pytest.raises((DBAPIError, IntegrityError)), engine.begin() as conn:
        conn.execute(stmt, {"m": MID})  # bypass the service: a second ACTIVE row


def test_stale_state_changes_are_rejected(world: Any) -> None:
    _store, svc, engine = world
    reg = SqlMemoryRegistryStore(engine)
    from experience_engine.ports.registry import TransitionSpec

    with pytest.raises(StaleStateError):
        reg.apply_transitions(
            tenant_id=TENANT, capability_id=MID,
            transitions=[TransitionSpec(1, S.VALIDATING, S.APPROVED)], pointer=None,
            actor="x", reason="stale", evaluation_id=None, policy_hash=None, now=NOW,
        )  # fmt: skip
    assert state_of(engine) == "CANDIDATE"
    with pytest.raises(StaleStateError):
        reg.apply_transitions(
            tenant_id=TENANT, capability_id="mem_unknown_x", transitions=[], pointer=None,
            actor="x", reason="r", evaluation_id=None, policy_hash=None, now=NOW,
        )  # fmt: skip
    del svc


def test_one_tenants_memory_is_never_visible_to_another(engine: Engine) -> None:
    from experience_engine.memory.service import build_memory

    store = SqlMemoryStore(engine)
    data = experience()
    for tenant in ("acme", "globex"):
        res = build_memory(
            tenant_id=tenant, trajectories=data, capabilities=[], evidence={}, now=datetime.now(UTC)
        )
        register_candidates(store, res.definitions)
    svc = MemoryRegistryService(SqlMemoryRegistryStore(engine), default_policies(), FakeClock())
    evaluate(svc, Verdict.PASS, tenant="acme")
    store_reg = SqlMemoryRegistryStore(engine)
    svc.start_shadow("acme", MID, 1, "ev")
    for i in range(5):
        svc.record_observation(
            store_reg.get_version("acme", MID, 1), "SHADOW", f"s{i}", True, True, 5, 9
        )  # type: ignore[arg-type]  # noqa: E501
    svc.advance_to_canary("acme", MID, 1, "ev")
    for i in range(5):
        svc.record_observation(
            store_reg.get_version("acme", MID, 1), "CANARY", f"c{i}", True, True, 5, 9
        )  # type: ignore[arg-type]  # noqa: E501
    svc.activate("acme", MID, 1, "ev")
    assert active_ids(engine, "acme") == [f"{MID}@1"]
    assert active_ids(engine, "globex") == [] and active_ids(engine, "initech") == []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(t.memory_versions.c.tenant_id, t.memory_versions.c.state).where(
                t.memory_versions.c.memory_id == MID
            )
        ).all()
    assert dict(rows) == {"acme": "ACTIVE", "globex": "CANDIDATE"}
    assert store_reg.get_version("globex", MID, 1) is not None
    assert store_reg.get_version("initech", MID, 1) is None
    with pytest.raises(StaleStateError):  # an unknown tenant cannot be transitioned at all
        store_reg.apply_transitions(
            tenant_id="initech", capability_id=MID, transitions=[], pointer=None, actor="x",
            reason="r", evaluation_id=None, policy_hash=None, now=NOW,
        )  # fmt: skip
