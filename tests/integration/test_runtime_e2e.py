"""M5 integration: fresh-session runtime use of capabilities against real PostgreSQL."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from test_learning_e2e import Env
from test_registry_e2e import (
    COMMITTED,
    Oracle,
    evaluate_version,
    learned,
    promote_to_active,
    service,
)

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.catalog import SqlExecutableCatalog
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.adapters.results import InMemoryResultStore
from experience_engine.bench.dataset import DatasetLoader, EvaluatorAccess, write_private_dataset
from experience_engine.bench.harness import run_benchmark
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import AgentConfig, BenchmarkRunRecord, ModelSettings, Split
from experience_engine.domain.capability import CapabilityState
from experience_engine.domain.decision import Strategy
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.experience import ExclusionReason
from experience_engine.execution.capability_runtime import CapabilityRuntime
from experience_engine.execution.controller import FixedPolicy, RuleCapabilityPolicy
from experience_engine.execution.matcher import CapabilityMatcher
from support import FakeClock

pytestmark = pytest.mark.integration


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    write_private_dataset(root, seed=987654321)
    return root


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Env:
    return Env(engine, tmp_path)


TENANT = "bench"
C_CONFIG = AgentConfig(
    config_id="C_CAPABILITIES",
    prompt_version="t",
    model=ModelSettings(provider="fake", model="heuristic-fake/1"),
)


def runtime_for(engine: Engine, states: set[CapabilityState] | None = None) -> CapabilityRuntime:
    catalog = SqlExecutableCatalog(engine, states) if states else SqlExecutableCatalog(engine)
    return CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: Oracle(),
        experimental=catalog.experimental,
    )


def serve(
    env: Env,
    split: Split,
    tasks: list,  # type: ignore[type-arg]
    runtime: CapabilityRuntime | None,
) -> list[BenchmarkRunRecord]:
    """A brand-new orchestrator per call: nothing carries over except the database."""
    from experience_engine.domain.benchmark import DatasetVersion

    return run_benchmark(
        tasks,
        dataset=DatasetVersion(
            name="m5", version="1", split=split, content_hash="h" * 64, n_tasks=len(tasks)
        ),
        seed=0,
        config=C_CONFIG,
        llm_factory=lambda _t, _s: HeuristicFakeLLM(),
        controller=RuleCapabilityPolicy() if runtime else FixedPolicy(),
        verifier=SimulationVerifier(),
        clock=FakeClock(),
        store=InMemoryResultStore(),
        lifecycle=env.lifecycle,
        artifacts=env.artifacts,
        capabilities=runtime,
    )


def hidden_tasks(private_root: Path) -> list:  # type: ignore[type-arg]
    loader = DatasetLoader(COMMITTED, EvaluatorAccess(), private_root=private_root)
    return [bt for bt in loader.load(Split.HIDDEN_TRANSFER).tasks if bt.family == "pool"]


def activate_pool(env: Env, private_root: Path) -> tuple[str, int]:
    caps = learned(env)
    svc = service(env)
    report = evaluate_version(env, private_root, caps["pool"], 1, Oracle(), svc)
    assert report.verdict == Verdict.PASS, report.reasons
    promote_to_active(env, svc, caps["pool"], 1)
    return caps["pool"], 1


def test_only_active_versions_are_executable_and_unpromoted_ones_are_invisible(
    env: Env,
) -> None:
    learned(env)  # versions exist (CANDIDATE) but nothing was evaluated or promoted
    assert SqlExecutableCatalog(env.engine).list_executable(TENANT) == []
    experimental = SqlExecutableCatalog(env.engine, {CapabilityState.CANDIDATE})
    assert experimental.experimental and len(experimental.list_executable(TENANT)) >= 1
    assert not SqlExecutableCatalog(env.engine).experimental


def test_default_runtime_serves_nothing_from_unpromoted_capabilities(env: Env) -> None:
    learned(env)
    task = hidden_tasks_free()[0]
    records = serve(env, Split.DISCOVERY, [task], runtime_for(env.engine))
    assert records[0].decision.selected_action == Strategy.GENERAL_PLANNING
    assert records[0].outcome.capability_version is None


def hidden_tasks_free() -> list:  # type: ignore[type-arg]
    from experience_engine.bench.dataset import generate_split

    return [bt for bt in generate_split(Split.DISCOVERY) if bt.family == "pool"]


def test_fresh_session_transfer_uses_only_the_database_catalog(
    env: Env, private_root: Path
) -> None:
    cap, version = activate_pool(env, private_root)
    tasks = hidden_tasks(private_root)
    assert len(tasks) >= 2

    baseline = serve(env, Split.HIDDEN_TRANSFER, tasks, None)  # no-learning arm A
    # a *new* runtime object built only from the database: no in-process state from learning
    with_cap = serve(env, Split.HIDDEN_TRANSFER, tasks, runtime_for(env.engine))

    assert sum(r.outcome.status.value == "VERIFIED_SUCCESS" for r in baseline) == 0
    assert all(r.outcome.status.value == "VERIFIED_SUCCESS" for r in with_cap)
    assert all(r.outcome.capability_version == f"{cap}@{version}" for r in with_cap)
    assert all(r.decision.selected_action == Strategy.DIRECT_CAPABILITY for r in with_cap)

    run_id = with_cap[0].run_id
    with env.engine.connect() as conn:
        outcome = conn.execute(
            sa.select(t.run_outcomes.c.capability_version, t.run_outcomes.c.config_id).where(
                t.run_outcomes.c.run_id == run_id
            )
        ).one()
        decision = conn.execute(
            sa.select(
                t.policy_decisions.c.policy_version,
                t.policy_decisions.c.selected_action,
                t.policy_decisions.c.selection_probability,
                t.policy_decisions.c.eligible_actions,
                t.policy_decisions.c.feature_snapshot,
                t.policy_decisions.c.outcome_status,
            ).where(t.policy_decisions.c.run_id == run_id)
        ).one()
        kinds = {
            r[0]
            for r in conn.execute(
                sa.select(t.trace_events.c.type).where(t.trace_events.c.run_id == run_id)
            )
        }
    assert tuple(outcome) == (f"{cap}@{version}", "C_CAPABILITIES")
    assert decision.policy_version == "rule-capability/1"
    assert decision.selected_action == "DIRECT_CAPABILITY" and decision.selection_probability == 1.0
    assert decision.eligible_actions == ["DIRECT_CAPABILITY", "GENERAL_PLANNING"]
    assert decision.feature_snapshot["selected_capability"] == f"{cap}@{version}"
    assert decision.outcome_status == "VERIFIED_SUCCESS"
    assert {"CAPABILITY_MATCH", "CAPABILITY_EXEC", "TOOL_CALL", "VERIFICATION"} <= kinds


def test_capability_assisted_runs_never_feed_the_learning_pipeline(
    env: Env, private_root: Path
) -> None:
    cap, version = activate_pool(env, private_root)
    served = serve(env, Split.DISCOVERY, hidden_tasks_free(), runtime_for(env.engine))
    assert all(r.outcome.capability_version == f"{cap}@{version}" for r in served)
    env.deliver_events()
    env.process_jobs()
    ids = {r.run_id for r in served}
    with env.engine.connect() as conn:
        rows = conn.execute(
            sa.select(t.experience_cases.c.eligible, t.experience_cases.c.excluded_reason).where(
                t.experience_cases.c.run_id.in_(ids)
            )
        ).all()
    assert len(rows) == len(ids)
    assert all(
        not eligible and reason == ExclusionReason.CAPABILITY_ASSISTED for eligible, reason in rows
    )


def test_capabilities_are_tenant_scoped_at_runtime(env: Env, private_root: Path) -> None:
    activate_pool(env, private_root)
    other = SqlExecutableCatalog(env.engine)
    assert other.list_executable("someone-else") == []
    assert other.get_executable("someone-else", "pool.0000000000", 1) is None
    assert len(other.list_executable(TENANT)) == 1
