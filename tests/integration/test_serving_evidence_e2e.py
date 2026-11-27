"""M6.2-A integration (real PostgreSQL): the real evaluation workflow records final-outcome paired
evidence; it never changes a promotion decision; a stored evaluation seeds an eligibility-checked
prior; ineligible evidence cannot produce a production prior."""

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from test_learning_e2e import Env
from test_registry_e2e import COMMITTED, TENANT, Oracle, learned, service

from experience_engine import controller_cli
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.registry_store import SqlRegistryStore
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.bench.dataset import DatasetLoader, EvaluatorAccess, write_private_dataset
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.registry import RegistryService
from experience_engine.domain.benchmark import AgentConfig, ModelSettings, Split
from experience_engine.domain.capability import CapabilityState as S
from experience_engine.domain.evaluation import Arm, EvaluationReport
from experience_engine.evaluation.arms import make_agent_arm, make_capability_arm
from experience_engine.evaluation.engine import EvaluationPlan, evaluate
from experience_engine.evaluation.experiment_arms import make_runtime_arm
from experience_engine.execution.capability_runtime import (
    CapabilityRuntime,
    StaticCapabilityCatalog,
)
from experience_engine.execution.matcher import CapabilityMatcher
from support import FakeClock

pytestmark = pytest.mark.integration


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    write_private_dataset(root, seed=987654321, scale=12)  # enough held-out tasks for >= 30 pairs
    return root


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Env:
    return Env(engine, tmp_path)


def plan_for(
    env: Env,
    private_root: Path,
    cap_id: str,
    serving: bool,
    provider: str = "fake",
    seeds: list[int] | None = None,
) -> tuple[EvaluationPlan, RegistryService]:
    svc = service(env)
    rec = SqlRegistryStore(env.engine).get_version(TENANT, cap_id, 1)
    assert rec is not None
    if rec.state == S.CANDIDATE:
        svc.start_validation(TENANT, cap_id, 1, "ev")
    loader = DatasetLoader(COMMITTED, EvaluatorAccess(), private_root=private_root)
    disc, val, hid = (
        loader.load(s) for s in (Split.DISCOVERY, Split.VALIDATION, Split.HIDDEN_TRANSFER)
    )
    verifier, clock, llm, decisions = (
        SimulationVerifier(),
        FakeClock(),
        HeuristicFakeLLM(),
        Oracle(),
    )
    model = ModelSettings(provider=provider, model="heuristic-fake/1")
    cfg = AgentConfig(prompt_version="t", model=model)
    arms = {
        Arm.BASELINE: make_agent_arm(llm=llm, config=cfg, verifier=verifier, clock=clock),
        Arm.MEMORY: make_agent_arm(llm=llm, config=cfg, verifier=verifier, clock=clock),
        Arm.CAPABILITY: make_capability_arm(
            definition=rec.definition,
            tenant_id=TENANT,
            decisions=decisions,
            verifier=verifier,
            clock=clock,
        ),
    }
    serving_arm = None
    if serving:
        catalog = StaticCapabilityCatalog(TENANT, [rec.definition])
        runtime = CapabilityRuntime(
            catalog=catalog,
            matcher=CapabilityMatcher(catalog),
            decisions_for=lambda _l: decisions,
            experimental=True,
        )
        serving_arm = make_runtime_arm(
            llm=llm, config=cfg, verifier=verifier, clock=clock, runtime=runtime, tenant_id=TENANT
        )
    plan = EvaluationPlan(
        tenant_id=TENANT,
        definition=rec.definition,
        policy=svc.policy_for(rec),
        clock=clock,
        arms=arms,
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
        seeds=seeds or [0],
        serving_arm=serving_arm,
    )
    return plan, svc


def stored(env: Env) -> list[dict[str, Any]]:
    with env.engine.connect() as conn:
        rows = conn.execute(sa.select(t.capability_evaluations)).mappings().all()
    return [dict(r) for r in rows]


def test_the_serving_arm_records_final_outcome_pairs_and_never_changes_the_promotion_decision(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    plain_plan, _ = plan_for(env, private_root, caps["pool"], serving=False)
    plain = evaluate(plain_plan)
    with_plan, svc = plan_for(env, private_root, caps["pool"], serving=True)
    served = evaluate(with_plan)
    # promotion inputs and outputs are identical; only the optional evidence differs
    assert plain.serving_evidence is None and served.serving_evidence is not None
    assert (served.verdict, served.reasons) == (plain.verdict, plain.reasons)
    assert served.suites == plain.suites and served.regressions == plain.regressions
    assert served.false_applicability_rate == plain.false_applicability_rate
    assert "serving_evidence" not in plain.model_dump(mode="json")
    ev = served.serving_evidence
    assert ev.capability_ref == f"{caps['pool']}@1" and ev.n >= 8
    assert ev.n + ev.n_tied_dropped + ev.n_unknown_dropped + ev.n_not_offered > 0
    assert ev.suites == ["HELD_OUT", "TRANSFER"] and ev.catalog == "EVALUATION_STATIC_CATALOG"
    assert ev.n_missing_cost == 0 and ev.n_duplicate_rows == 0
    # its tokens are counted as validation cost, never hidden
    assert served.resources is not None and Arm.SERVING.value in served.resources.by_arm
    assert served.evaluation_config_hash != plain.evaluation_config_hash  # the arm is in the config
    svc.complete_evaluation(served, "ev")
    (row,) = stored(env)
    assert row["metrics"]["serving_evidence"]["n"] == ev.n
    assert row["verdict"] == served.verdict.value  # the stored decision is the promotion decision


def test_a_stored_evaluation_without_the_arm_is_byte_compatible(
    env: Env, private_root: Path
) -> None:
    caps = learned(env)
    plan, svc = plan_for(env, private_root, caps["pool"], serving=False)
    report = evaluate(plan)
    svc.complete_evaluation(report, "ev")
    (row,) = stored(env)
    assert "serving_evidence" not in row["metrics"]
    assert EvaluationReport.model_validate(json.loads(report.model_dump_json())) == report  # loads


@pytest.fixture
def cli(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("EE_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("EE_ARTIFACT_ROOT", str(tmp_path / "a"))

    def run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = controller_cli.main(list(argv))
        return code, capsys.readouterr().out

    return run


def seed_evaluation(env: Env, private_root: Path, provider: str) -> tuple[str, EvaluationReport]:
    caps = learned(env)
    plan, svc = plan_for(env, private_root, caps["pool"], serving=True, provider=provider)
    report = evaluate(plan)
    svc.complete_evaluation(report, "ev")
    return caps["pool"], report


def test_a_synthetic_evaluation_cannot_produce_a_production_prior_but_can_be_taken_as_research(
    env: Env, private_root: Path, cli: Any, engine: Engine
) -> None:
    cap, report = seed_evaluation(env, private_root, "fake")
    ref = f"{cap}@1"
    with pytest.raises(SystemExit, match="no production-eligible prior"):
        cli(
            "train",
            "--kind",
            "evidence",
            "--paired-from-registry",
            ref,
            "--n-min-pairs-unused" if False else "--n-min",
            "30",
        )
    with engine.connect() as conn:
        assert (
            conn.execute(sa.select(sa.func.count()).select_from(t.controller_policies)).scalar_one()
            == 0
        )
    code, out = cli(
        "train", "--kind", "evidence", "--paired-from-registry", ref, "--allow-research"
    )
    assert code == 0 and "NOT eligible" in out and "synthetic" in out
    with engine.connect() as conn:
        d = conn.execute(sa.select(t.controller_policies.c.definition)).scalar_one()
    (src,) = d["snapshot"]["prior_sources"]
    assert src["production_eligible"] is False and src["evidence_kind"] == "PROMOTION_EVALUATION"
    assert (
        d["params"]["evidence"].get("require_production_eligible") is None
    )  # research: not required
    assert report.serving_evidence is not None


def test_a_valid_real_model_evaluation_produces_an_eligible_prior_with_full_provenance(
    env: Env, private_root: Path, cli: Any, engine: Engine
) -> None:
    cap, report = seed_evaluation(env, private_root, "fake")
    ref = f"{cap}@1"
    store = SqlRegistryStore(env.engine)
    relabelled = report.model_copy(
        update={"model_settings": {"provider": "vertex", "model": "gemini-2.5-flash"}}
    )  # the same measurements recorded as a real-model evaluation (test of the gates only)
    store.record_evaluation(relabelled, "f" * 64, FakeClock().now())
    code, out = cli("train", "--kind", "evidence", "--paired-from-registry", ref)
    assert report.serving_evidence is not None and report.serving_evidence.n >= 30
    assert code == 0 and "ELIGIBLE" in out, out
    with engine.connect() as conn:
        d = conn.execute(sa.select(t.controller_policies.c.definition)).scalar_one()
    (src,) = d["snapshot"]["prior_sources"]
    assert src["production_eligible"] is True and src["tenant_id"] == TENANT
    assert src["evaluation_id"] and src["report_sha256"] and src["runtime_version"]
    assert d["params"]["evidence"]["require_production_eligible"] is True
    code, out = cli("evidence-inspect", "--version", "1")
    assert code == 0 and '"eligible": true' in out


def test_a_missing_serving_arm_is_a_clear_error(env: Env, private_root: Path, cli: Any) -> None:
    caps = learned(env)
    plan, svc = plan_for(env, private_root, caps["pool"], serving=False)
    svc.complete_evaluation(evaluate(plan), "ev")
    with pytest.raises(SystemExit, match="serving-evidence"):
        cli("train", "--kind", "evidence", "--paired-from-registry", f"{caps['pool']}@1")
