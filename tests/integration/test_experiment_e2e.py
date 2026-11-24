"""ee-exp against real PostgreSQL: experience loading, the ledger from recorded costs, the CLI,
and the guarantee that an experiment never touches the registry."""

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
