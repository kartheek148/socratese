"""ee-exp against real PostgreSQL: experience loading, the ledger from recorded costs, the CLI,
and the guarantee that an experiment never touches the registry."""

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from test_learning_e2e import Env
from test_registry_e2e import COMMITTED, Oracle, evaluate_version, learned, service

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.experience_store import SqlExperienceStore
from experience_engine.bench.dataset import write_private_dataset
from experience_engine.domain.experience import ExperienceCase
from experience_engine.domain.experiment import ExperimentResult
from experience_engine.experiment_cli import build_ledger, load_discovery_experience, main
from experience_engine.memory.bank import ExperienceMemory

pytestmark = pytest.mark.integration
TENANT = "bench"


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    write_private_dataset(root, seed=424242)
    return root


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Env:
    return Env(engine, tmp_path)


def states(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                t.capability_versions.c.capability_id,
                t.capability_versions.c.version,
                t.capability_versions.c.state,
            ).order_by(t.capability_versions.c.capability_id)
        ).all()
    return [tuple(r) for r in rows]


def test_only_eligible_discovery_trajectories_are_loaded_and_memory_is_built_from_them(
    env: Env,
) -> None:
    learned(env)
    trajs = load_discovery_experience(SqlExperienceStore(env.engine), env.artifacts, TENANT)
    assert len(trajs) == 12 and {x.task_family for x in trajs} == {"pool", "timeout"}
    mem = ExperienceMemory(trajs)
    assert mem.families == ["pool", "timeout"]


def test_loader_refuses_a_case_that_is_not_discovery_experience() -> None:
    class Store:
        def list_eligible_cases(self, tenant: str, pipeline: str) -> list[ExperienceCase]:
            return [
                ExperienceCase.model_construct(
                    case_id="c1", dataset_split="VALIDATION", trajectory_sha256="x" * 64
                )
            ]

    with pytest.raises(RuntimeError, match="not usable discovery experience"):
        load_discovery_experience(Store(), None, TENANT)  # type: ignore[arg-type]


def test_ledger_is_built_from_recorded_costs_and_evaluation_records(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    trajs = load_discovery_experience(SqlExperienceStore(env.engine), env.artifacts, TENANT)
    defs = [d for d, _ in env.caps.list_versions(TENANT)]
    before, _ = build_ledger(env.engine, TENANT, trajs, defs, consolidation_ms=7)
    assert before.discovery_tokens == sum(x.tokens_used for x in trajs) > 0
    assert before.discovery_runs == 12 and before.validation_tokens == 0
    assert any("no evaluation record" in n for n in before.notes)
    assert before.wall_ms_consolidation == 7 and before.compilation_tokens == 0

    report = evaluate_version(env, private_root, caps["pool"], 1, Oracle(), service(env))
    pool_def = next(d for d in defs if d.id == caps["pool"])
    after, by_family = build_ledger(env.engine, TENANT, trajs, [pool_def], consolidation_ms=0)
    life = report.lifecycle
    assert after.validation_tokens == life.evaluation_overhead_tokens + life.capability_exec_tokens
    assert after.validation_tokens > 0 and by_family["pool"].validation_tokens > 0
    assert by_family["timeout"].validation_tokens == 0
    assert by_family["pool"].discovery_runs == 6


def test_cli_runs_all_five_arms_on_identical_tasks_and_never_changes_the_registry(
    env: Env,
    private_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caps = learned(env)
    snapshot = states(env.engine)
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    monkeypatch.setenv("EE_DATABASE_URL", env.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    out, report = tmp_path / "res.json", tmp_path / "report.md"
    rc = main(
        [
            "run", "--tenant", TENANT, "--root", str(COMMITTED),
            "--private-root", str(private_root),
            "--capabilities", f"{caps['pool']}@1", f"{caps['timeout']}@1",
            "--llm", "fake", "--reps", "2", "--workers", "4", "--max-tasks", "6",
            "--name", "plumbing", "--out", str(out), "--report", str(report),
        ]
    )  # fmt: skip
    assert rc == 0, capsys.readouterr().err
    result = ExperimentResult.model_validate_json(out.read_text())
    assert result.config.arms == list("ABCDE") and result.config.capability_catalog_states == [
        "CANDIDATE"
    ]
    assert len(result.rows) == 6 * 2 * 5
    for arm in "ABCDE":  # every arm saw exactly the same (task, repetition) grid
        assert {(r.task_id, r.rep) for r in result.rows if r.arm == arm} == {
            (r.task_id, r.rep) for r in result.rows if r.arm == "A"
        }
    assert "seed" not in result.config.model  # the provider ignores seeds: do not imply control
    assert all(r.retrieved_tokens > 0 for r in result.rows if r.arm in "BCD")
    assert all(r.retrieved_tokens == 0 for r in result.rows if r.arm in "AE")
    assert any(r.capability_used for r in result.rows if r.arm == "E")
    assert result.ledger.discovery_runs == 12 and result.applicability
    assert "EXPERIMENTAL" in report.read_text().upper()
    assert states(env.engine) == snapshot  # promotion state is never changed by an experiment

    out2 = tmp_path / "report2.md"
    assert main(["report", "--results", str(out), "--out", str(out2), "--title", "again"]) == 0
    assert "# again" in out2.read_text()


def test_cli_refuses_without_evaluator_access_and_without_private_data(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from experience_engine.bench.dataset import SplitAccessError

    monkeypatch.delenv("EE_EVALUATOR_ROLE", raising=False)
    monkeypatch.delenv("EE_PRIVATE_DATASET_ROOT", raising=False)
    args = ["run", "--root", str(COMMITTED), "--capabilities", "x.0123456789@1", "--out", "o"]
    with pytest.raises(SplitAccessError):
        main(args)
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    assert main(args) == 2  # no fresh private data supplied


def test_gated_cli_reads_only_active_assets_and_records_the_exact_version_or_the_reason(
    env: Env,
    private_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from test_memory_governance import built
    from test_memory_governance_e2e import evaluate as evaluate_memory
    from test_memory_governance_e2e import promote as promote_memory

    from experience_engine.adapters.db.memory_registry_store import SqlMemoryRegistryStore
    from experience_engine.adapters.db.memory_store import SqlMemoryStore
    from experience_engine.domain.evaluation import Verdict, default_policies
    from experience_engine.governance.memory import MemoryRegistryService
    from experience_engine.memory.service import register_candidates
    from support import FakeClock

    learned(env)
    trajs = load_discovery_experience(SqlExperienceStore(env.engine), env.artifacts, TENANT)
    register_candidates(SqlMemoryStore(env.engine), built(trajs))
    svc = MemoryRegistryService(SqlMemoryRegistryStore(env.engine), default_policies(), FakeClock())
    # ACTIVE: episodic/pool; REJECTED: consolidated/pool; CANDIDATE: everything else
    evaluate_memory(svc, Verdict.PASS, mid="mem_episodic_pool")
    promote_memory(svc, env.engine, mid="mem_episodic_pool")
    evaluate_memory(svc, Verdict.FAIL, mid="mem_consolidated_pool")

    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    monkeypatch.setenv("EE_DATABASE_URL", env.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    out, report = tmp_path / "gated.json", tmp_path / "gated.md"
    rc = main(
        [
            "run", "--mode", "gated", "--tenant", TENANT, "--root", str(COMMITTED),
            "--private-root", str(private_root), "--llm", "fake", "--reps", "1",
            "--workers", "4", "--max-tasks", "6", "--name", "gated-plumbing",
            "--out", str(out), "--report", str(report),
        ]
    )  # fmt: skip
    assert rc == 0, capsys.readouterr().err
    result = ExperimentResult.model_validate_json(out.read_text())
    assert result.config.mode == "gated" and result.config.asset_states == ["ACTIVE"]
    assert result.config.memory_assets == ["mem_episodic_pool@1"]
    assert set(result.config.arms) == set("ABCDEF")
    used = {(r.arm, r.asset_ref) for r in result.rows if r.asset_ref}
    assert used <= {("B", "mem_episodic_pool@1")}  # nothing CANDIDATE/REJECTED was ever served
    for r in result.rows:
        if r.arm in "CDF" or (r.arm == "B" and r.family != "pool"):
            assert r.asset_ref is None and r.fallback_reason and r.retrieved_tokens == 0
        if r.arm == "B" and r.family == "pool":
            assert r.asset_ref == "mem_episodic_pool@1" and r.asset_family == "pool"
    assert any(r.arm == "B" and r.asset_ref for r in result.rows)
    assert "6b. Asset usage" in report.read_text()


def test_serving_cli_attributes_phases_serves_only_active_and_honours_strategy_approval(
    env: Env,
    private_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from test_memory_governance import built
    from test_memory_governance_e2e import evaluate as evaluate_memory
    from test_memory_governance_e2e import promote as promote_memory

    from experience_engine.adapters.db.memory_registry_store import SqlMemoryRegistryStore
    from experience_engine.adapters.db.memory_store import SqlMemoryStore
    from experience_engine.domain.evaluation import Verdict, default_policies
    from experience_engine.governance.memory import MemoryRegistryService
    from experience_engine.memory.service import register_candidates
    from support import FakeClock

    learned(env)
    trajs = load_discovery_experience(SqlExperienceStore(env.engine), env.artifacts, TENANT)
    register_candidates(SqlMemoryStore(env.engine), built(trajs))
    svc = MemoryRegistryService(SqlMemoryRegistryStore(env.engine), default_policies(), FakeClock())
    evaluate_memory(svc, Verdict.PASS, mid="mem_episodic_pool")  # ACTIVE
    promote_memory(svc, env.engine, mid="mem_episodic_pool")
    evaluate_memory(svc, Verdict.FAIL, mid="mem_consolidated_pool")  # REJECTED
    snapshot = states(env.engine)

    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    monkeypatch.setenv("EE_DATABASE_URL", env.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    def run(name: str, *extra: str) -> ExperimentResult:
        out = tmp_path / f"{name}.json"
        rc = main(
            ["run", "--mode", "gated", "--attribute-phases", "--tenant", TENANT,
             "--root", str(COMMITTED), "--private-root", str(private_root), "--llm", "fake",
             "--reps", "1", "--workers", "4", "--max-tasks", "8", "--name", name,
             "--out", str(out), *extra]
        )  # fmt: skip
        assert rc == 0, capsys.readouterr().err
        return ExperimentResult.model_validate_json(out.read_text())

    prod = run(
        "production"
    )  # ACTIVE only: the REJECTED memory and CANDIDATE capabilities are inert
    assert prod.config.asset_states == ["ACTIVE"]
    used = {(r.arm, r.asset_ref) for r in prod.rows if r.asset_ref}
    assert used <= {("B", "mem_episodic_pool@1")} and used
    assert all(r.strategy is not None for r in prod.rows if r.arm in "BCDEF")
    for r in prod.rows:
        s = r.strategy
        if r.arm == "E" and s is not None:  # no ACTIVE capability: plain planning, no phases
            assert s.strategy_id == "planning" and s.capability is None and not s.fallback.triggered
        if r.arm == "B" and r.asset_ref and s is not None:
            assert s.strategy_id == "memory-assisted" and s.asset_kind == "MEMORY"
            assert s.phases_consistent() and s.total_tokens == r.tokens

    # candidate access only via the explicit experimental mechanism, and approval can still block it
    research = run("research", "--experimental-states", "ACTIVE", "CANDIDATE", "REJECTED")
    caps = {r.asset_ref for r in research.rows if r.arm == "E" and r.asset_ref}
    assert caps and research.config.asset_states == ["ACTIVE", "CANDIDATE", "REJECTED"]
    blocked = run(
        "blocked", "--experimental-states", "ACTIVE", "CANDIDATE", "REJECTED", "--approved"
    )
    assert not [r for r in blocked.rows if r.asset_ref]  # nothing approved: nothing used
    assert states(env.engine) == snapshot  # experiments never change registry state

    report = tmp_path / "serving.md"
    rj = tmp_path / "serving.json"
    assert main(["serving-report", "--set", "S", str(tmp_path / "research.json"),
                 "--out", str(report), "--json-out", str(rj)]) == 0  # fmt: skip
    text = report.read_text()
    assert "Experimental asset states" in text and "E1 capability only (derived)" in text
    assert json.loads(rj.read_text()) is not None
