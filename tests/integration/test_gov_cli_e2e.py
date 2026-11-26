"""ee-learn build-memory and ee-gov plumbing against real PostgreSQL (fake model)."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from test_learning_e2e import Env
from test_registry_e2e import COMMITTED, learned

from experience_engine import gov_cli, learn_cli
from experience_engine.adapters.db import tables as t
from experience_engine.bench.dataset import write_private_dataset

pytestmark = pytest.mark.integration
TENANT = "bench"


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Env:
    return Env(engine, tmp_path)


def rows(engine: Engine, table: sa.Table, *cols: sa.Column) -> list[tuple]:  # type: ignore[type-arg]
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.select(*cols).select_from(table)).all()]


def test_build_memory_registers_candidates_only_and_records_its_build_cost(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    learned(env)
    monkeypatch.delenv("EE_EVALUATOR_ROLE", raising=False)
    monkeypatch.setenv("EE_DATABASE_URL", env.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    assert learn_cli.main(["build-memory", "--tenant", TENANT]) == 0
    assert learn_cli.main(["build-memory", "--tenant", TENANT]) == 0  # idempotent
    states = rows(env.engine, t.memory_versions, t.memory_versions.c.memory_id,
                  t.memory_versions.c.state)  # fmt: skip
    assert {s for _, s in states} == {"CANDIDATE"}
    assert {m for m, _ in states} >= {"mem_episodic_pool", "mem_consolidated_timeout"}
    assert len(states) == len({m for m, _ in states})  # no duplicate versions on re-run
    costs = rows(env.engine, t.cost_ledger, t.cost_ledger.c.subject_type, t.cost_ledger.c.tokens)
    assert ("memory_build", 0) in costs


def test_learning_plane_cannot_build_memory_with_evaluator_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    assert learn_cli.main(["build-memory", "--tenant", TENANT]) == 3


def test_gov_evaluate_decides_promote_requires_approval_and_status_lists_versions(
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    learned(env)
    private = tmp_path / "private"
    write_private_dataset(private, seed=424242)
    monkeypatch.delenv("EE_EVALUATOR_ROLE", raising=False)
    monkeypatch.setenv("EE_DATABASE_URL", env.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    assert learn_cli.main(["build-memory", "--tenant", TENANT]) == 0

    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    mid = "mem_consolidated_pool@1"
    code = gov_cli.main(
        ["evaluate", "--tenant", TENANT, "--memory", mid, "--root", str(COMMITTED),
         "--private-root", str(private), "--llm", "fake", "--seeds", "0", "--workers", "2"]
    )  # fmt: skip
    assert code in (0, 1)
    (state,) = [
        s for m, s in rows(env.engine, t.memory_versions, t.memory_versions.c.memory_id,
                           t.memory_versions.c.state) if m == "mem_consolidated_pool"
    ]  # fmt: skip
    assert state in ("APPROVED", "REJECTED", "NEEDS_REVIEW")  # decided by the policy, not by us
    assert rows(env.engine, t.memory_evaluations, t.memory_evaluations.c.memory_id)
    # an unevaluated candidate can never be promoted
    assert gov_cli.main(["promote", "--tenant", TENANT, "--memory", "mem_episodic_pool@1",
                         "--to", "active"]) == 2  # fmt: skip
    capsys.readouterr()
    assert gov_cli.main(["status", "--tenant", TENANT]) == 0
    assert "MEMORY     mem_episodic_pool@1" in capsys.readouterr().out


def test_gov_refuses_without_evaluator_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EE_EVALUATOR_ROLE", raising=False)
    assert gov_cli.main(["status", "--tenant", TENANT]) == 3


def test_gov_evaluate_with_candidate_policy_cache_stages_and_counterfactual(
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    learned(env)
    private = tmp_path / "private"
    write_private_dataset(private, seed=424242)
    monkeypatch.delenv("EE_EVALUATOR_ROLE", raising=False)
    monkeypatch.setenv("EE_DATABASE_URL", env.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    assert learn_cli.main(["build-memory", "--tenant", TENANT]) == 0
    monkeypatch.setenv("EE_EVALUATOR_ROLE", "evaluator")
    cache = tmp_path / "baseline-cache"

    def run(asset: str) -> str:
        capsys.readouterr()
        code = gov_cli.main(
            ["--policy-set", "risk-v2", "evaluate", "--tenant", TENANT, "--memory", asset,
             "--root", str(COMMITTED), "--private-root", str(private), "--llm", "fake",
             "--seeds", "0", "--workers", "2", "--baseline-cache", str(cache), "--staged",
             "--counterfactual-policy-set", "default"]
        )  # fmt: skip
        assert code in (0, 1)
        return capsys.readouterr().out

    first = run("mem_consolidated_pool@1")
    assert "counterfactual default-low:" in first and "(not acted on)" in first
    assert "baseline cache: hits=0" in first and "resources A_BASELINE:" in first
    second = run("mem_procedural_pool@1")  # same family, new asset: baseline comes from the cache
    assert "baseline cache: hits=0" not in second and "misses=0" in second
    rows = rows_of(env.engine)
    assert {r[0] for r in rows} == {"default-low-v2"}  # the governing policy was recorded


def rows_of(engine: Engine) -> list[tuple]:  # type: ignore[type-arg]
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.select(t.memory_evaluations.c.policy_name)).all()]
