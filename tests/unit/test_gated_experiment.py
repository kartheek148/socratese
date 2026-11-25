"""M5.8: gated experiment arms, fair lifecycle economics and the gated report."""

from typing import Any

import pytest
from test_experiment import cfg, econ_rows, row
from test_memory_governance import pool_memory

from experience_engine.adapters.db.memory_catalog import StaticMemoryCatalog
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.bench.dataset import DEFAULT_SEED, generate_split
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import Split
from experience_engine.domain.experiment import (
    ExperimentConfig,
    ExperimentResult,
    LearningLedger,
)
from experience_engine.domain.memory import MemoryType
from experience_engine.evaluation import economics
from experience_engine.evaluation.experiment_arms import make_gated_memory_arm
from experience_engine.evaluation.experiment_report import render_experiment
from support import FakeClock, first_task

TENANT = "bench"


def gated_arm(catalog: StaticMemoryCatalog, kind: MemoryType = MemoryType.CONSOLIDATED) -> Any:
    return make_gated_memory_arm(
        llm=HeuristicFakeLLM(), config=cfg("C"), verifier=SimulationVerifier(), clock=FakeClock(),
        catalog=catalog, tenant_id=TENANT, memory_type=kind,
    )  # fmt: skip


def test_gated_arm_applies_only_the_active_asset_of_its_type_and_records_the_exact_version() -> (
    None
):
    out = gated_arm(StaticMemoryCatalog(TENANT, [pool_memory()]))(first_task())
    assert out.verified and out.applicable
    assert (out.asset_kind, out.asset_ref, out.asset_family) == (
        "MEMORY",
        "mem_consolidated_pool@1",
        "pool",
    )
    assert out.fallback_reason is None and out.retrieved_tokens > 0


def test_gated_arm_with_nothing_active_is_general_planning_with_a_recorded_reason() -> None:
    out = gated_arm(StaticMemoryCatalog(TENANT, []))(first_task())
    assert out.verified and not out.applicable and out.retrieved_tokens == 0
    assert out.asset_ref is None and "no ACTIVE CONSOLIDATED memory" in (out.fallback_reason or "")
    wrong_type = gated_arm(StaticMemoryCatalog(TENANT, [pool_memory(MemoryType.EPISODIC)]))(
        first_task()
    )
    assert wrong_type.asset_ref is None  # an ACTIVE memory of another type is not this arm's


def test_gated_arm_refuses_a_memory_outside_its_declared_family() -> None:
    other = next(t for t in generate_split(Split.DISCOVERY, DEFAULT_SEED) if t.family == "timeout")
    out = gated_arm(StaticMemoryCatalog(TENANT, [pool_memory()]))(other)
    assert out.asset_ref is None and out.asset_family is None  # no false application
    assert "no applicable ACTIVE" in (out.fallback_reason or "")


# ---- economics ------------------------------------------------------------------------------
GATED = LearningLedger(
    discovery_tokens=10_000,
    validation_by_arm={"B": 2_000, "C": 4_000, "D": 6_000, "E": 8_000},
    validation_runs_by_arm={"B": 2, "C": 2, "D": 2, "E": 2},
)


def rows_all() -> list[Any]:
    out = econ_rows()
    out += [row("F", f"t{i}", 0, i < 2, tokens=1000) for i in range(10)]
    out += [row("UB", f"t{i}", 0, i < 2, tokens=1100) for i in range(10)]
    return out


def test_as_built_charges_each_arm_its_own_recorded_validation_rejected_attempts_included() -> None:
    t = economics.lifecycle_table(rows_all(), GATED, arms=["A", "B", "E", "F", "UB"])
    assert t["B"].learning_tokens == 12_000 and t["E"].learning_tokens == 18_000
    assert t["F"].learning_tokens == 0 and t["F"].breakeven_note == "reference"
    assert t["UB"].learning_tokens == 10_000  # ungated memory is never validated


def test_equal_governance_charges_every_learning_arm_the_mean_recorded_validation() -> None:
    t = economics.lifecycle_table(
        rows_all(), GATED, arms=["A", "B", "E", "UB"], charge_validation_to_all=True
    )
    assert t["B"].learning_tokens == t["E"].learning_tokens == 15_000
    assert t["UB"].learning_tokens == 10_000 and t["A"].learning_tokens == 0


def test_revalidations_are_an_explicit_assumption_that_delays_break_even() -> None:
    plain = economics.lifecycle_table(rows_all(), GATED, arms=["A", "E"])["E"]
    r3 = economics.lifecycle_table(rows_all(), GATED, arms=["A", "E"], revalidations=3)["E"]
    assert r3.maintenance_tokens == 3 * 8_000 and plain.maintenance_tokens == 0
    assert r3.breakeven_successes > plain.breakeven_successes  # type: ignore[operator]
    none = economics.lifecycle_table(rows_all(), GATED, arms=["A", "F"], revalidations=3)
    assert none["F"].maintenance_tokens == 0


# ---- report ---------------------------------------------------------------------------------
def gated_result() -> ExperimentResult:
    rows: list[Any] = []
    for i in range(12):
        fam = "pool" if i % 2 == 0 else "queue"
        for arm in ("A", "B", "C", "D", "E", "F", "UB"):
            ok = (i + sum(map(ord, arm))) % 3 != 0
            in_family = fam == "pool" and arm in "BCDE"
            rows.append(row(
                arm, f"t{i}", 0, ok, tokens=300 if arm == "E" else 900, family=fam,
                level="intra_variant" if fam == "pool" else "cross_family",
                applicable=in_family if arm in "BCDE" else None,
                asset_ref=f"x_{fam}@1" if in_family else None,
                asset_family=fam if in_family else None,
                fallback_reason=None if in_family or arm in ("A", "F", "UB") else "no ACTIVE here",
                termination="FINAL",
            ))  # fmt: skip
    config = ExperimentConfig(
        name="g", model={"provider": "fake", "model": "m", "temperature": 0.0},
        arms=["A", "B", "C", "D", "E", "F", "UB"], repetitions=1, workers=1,
        datasets={"VALIDATION": {"name": "d", "version": "1", "content_hash": "a" * 64}},
        discovery_trajectories=10, discovery_families=["pool"], capabilities=["pool.x@1"],
        capability_catalog_states=["ACTIVE"], memory_preamble_sha256="b" * 64, max_episodes=3,
        verifier_version="v", runtime_version="r", evaluator_version="e", mode="gated",
        memory_assets=["mem_consolidated_pool@1"], asset_states=["ACTIVE"],
        ungated_families=["queue"],
    )  # fmt: skip
    return ExperimentResult(
        config=config, ledger=GATED, ledger_by_family={"pool": GATED}, rows=rows, applicability=[]
    )


def test_gated_report_shows_usage_fallbacks_negative_transfer_and_equal_governance() -> None:
    text = render_experiment(gated_result(), title="G")
    for needle in (
        "Gated mode.",
        "ACTIVE episodic memory only",
        "## 6b. Asset usage, fallback and false application",
        "## 7b. Negative transfer: what gating prevents",
        "### 5.2 Equal governance",
        "### 5.4 Sensitivity to maintenance",
        "ACTIVE memory assets: mem_consolidated_pool@1",
        "no ACTIVE here",
    ):
        assert needle in text, needle
    assert "| F noise control" in text
    assert render_experiment(gated_result(), title="G") == text


def test_legacy_report_has_no_gated_sections() -> None:
    from test_experiment import synthetic_result

    text = render_experiment(synthetic_result(), title="T")
    assert "6b. Asset usage" not in text and "Gated mode." not in text


@pytest.mark.parametrize("arm", ["B", "C", "D"])
def test_false_application_is_counted_when_an_asset_serves_a_foreign_family(arm: str) -> None:
    result = gated_result()
    bad = [
        r.model_copy(update={"asset_ref": "x_pool@1", "asset_family": "pool"})
        if r.arm == arm and r.family == "queue"
        else r
        for r in result.rows
    ]
    text = render_experiment(result.model_copy(update={"rows": bad}), title="G")
    line = next(ln for ln in text.splitlines() if ln.startswith(f"| {arm} ") and "| queue |" in ln)
    assert "| 6/6 | 0/6 | 6/6 |" in line  # applied, no fallback, all false applications


def test_report_flags_arms_that_never_applied_an_asset_as_equivalent_to_the_noise_control() -> None:
    result = gated_result()
    rows = [r.model_copy(update={"asset_ref": None}) if r.arm == "B" else r for r in result.rows]
    text = render_experiment(result.model_copy(update={"rows": rows}), title="G")
    assert "**Arms B applied no asset on any run**" in text
    assert "**Arms B applied" not in render_experiment(result, title="G")
