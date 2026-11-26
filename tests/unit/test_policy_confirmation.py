"""M5.9 confirmation analysis: truth labels, portfolios, stage replay, cost reconciliation."""

from typing import Any

import pytest
from test_experiment import row
from test_policy_v2 import LOW2, base_runner, big_plan, cap_runner, good_arms

from experience_engine.domain.evaluation import Arm, EvaluationReport, Verdict
from experience_engine.domain.experiment import RunRow
from experience_engine.evaluation.engine import evaluate
from experience_engine.evaluation.policy_confirmation import (
    AssetDecision,
    asset_cost,
    classify,
    compare_portfolios,
    decisions_from_reports,
    portfolio_rows,
    promoted_by_policy,
    replay_stages,
    truth_label,
)
from experience_engine.evaluation.policy_confirmation_report import SetInputs, render_confirmation
from experience_engine.evaluation.sequential import StagePlan
from experience_engine.evaluation.validation_cost import total_executed_tokens

ASSET = "mem_procedural_pool@1"


def rows_for(
    base_ok: list[bool], asset_ok: list[bool], *, ref: str | None = ASSET, arm: str = "D"
) -> list[RunRow]:
    out: list[RunRow] = []
    for i, (b, a) in enumerate(zip(base_ok, asset_ok, strict=True)):
        out.append(row("A", f"t{i}", 0, b, family="pool", variant="v"))
        out.append(row(arm, f"t{i}", 0, a, family="pool", variant="v", asset_ref=ref))
    return out


def test_truth_labels_follow_the_frozen_definitions() -> None:
    n = 40
    good = truth_label(rows_for([i % 3 == 0 for i in range(n)], [True] * n), ASSET, "D")
    assert good.label == "beneficial" and good.diff_lcb > 0
    bad = truth_label(rows_for([True] * n, [i % 4 == 0 for i in range(n)]), ASSET, "D")
    assert bad.label == "harmful" and bad.regression_rate is not None and bad.regression_rate > 0.5
    flat = [i % 2 == 0 for i in range(n)]
    assert truth_label(rows_for(flat, flat), ASSET, "D").label == "unclear"
    # credibly worse than the baseline is harmful even with a low regression rate
    assert truth_label(rows_for([True] * n, [i % 10 != 0 for i in range(n)]), ASSET, "D").label == (
        "harmful"
    )
    # beneficial needs a demonstrated gain: matching a baseline that is already perfect is unclear
    assert truth_label(rows_for([True] * n, [True] * n), ASSET, "D").label == "unclear"
    assert truth_label(rows_for([True] * 4, [True] * 4, ref="other@1"), ASSET, "D").tasks == 0


def test_portfolio_serves_capability_then_memory_then_baseline_for_the_exact_asset_only() -> None:
    rows = [
        row("A", "t0", 0, False, family="pool"),
        row("E", "t0", 0, True, family="pool", asset_ref="pool.x@1"),
        row("D", "t0", 0, False, family="pool", asset_ref="mem_procedural_pool@1"),
        row("A", "t1", 0, True, family="pool"),
        row("D", "t1", 0, False, family="pool", asset_ref="mem_procedural_pool@1"),
        row("A", "t2", 0, True, family="timeout"),
        row("D", "t2", 0, False, family="timeout", asset_ref="mem_procedural_timeout@1"),
        row("A", "t3", 0, True, family="pool"),
        row("D", "t3", 0, False, family="pool", asset_ref="mem_procedural_pool@2"),
    ]
    promoted = {"pool": [("E", "pool.x@1"), ("D", "mem_procedural_pool@1")]}
    served = portfolio_rows(rows, promoted, "P")
    # t0: capability; t1: memory (a regression); t2: nothing promoted -> baseline;
    # t3: the memory row came from a different version, so the baseline is served
    assert {r.task_id: r.verified for r in served} == {
        "t0": True,
        "t1": False,
        "t2": True,
        "t3": True,
    }
    assert {r.arm for r in served} == {"P"}


def test_promoting_a_beneficial_asset_wins_at_task_level_without_more_regressions() -> None:
    n = 40
    rows = rows_for([i % 3 == 0 for i in range(n)], [True] * n)
    overall, pool = compare_portfolios(rows, {}, {"pool": [("D", ASSET)]})  # v1 promotes nothing
    assert overall.scope == "all families" and pool.scope == "pool"
    assert overall.success_v1 == pytest.approx(overall.success_baseline)
    assert overall.success_v2 == pytest.approx(1.0) and overall.diff > 0.5 and overall.ci_lo > 0
    assert overall.only_v2 > 0 and overall.only_v1 == 0 and overall.p_mcnemar < 0.001
    assert overall.p_holm >= overall.p_mcnemar
    assert overall.reg_v1 == pytest.approx(0.0) and overall.reg_v2 == pytest.approx(0.0)
    assert overall.reg_diff == pytest.approx(0.0)


def test_promoting_a_harmful_asset_shows_a_higher_regression_rate() -> None:
    n = 40
    rows = rows_for([True] * n, [i % 4 == 0 for i in range(n)])
    c = compare_portfolios(rows, {}, {"pool": [("D", ASSET)]})[0]
    assert c.reg_v1 == pytest.approx(0.0) and c.reg_v2 is not None and c.reg_v2 > 0.5
    assert c.reg_ci_lo is not None and c.reg_ci_lo > 0 and c.reg_only_v2_fail > 20


def report_for(kind: str) -> EvaluationReport:
    if kind == "good":
        arms = good_arms()
    else:
        arms = {
            Arm.BASELINE: base_runner({"sqlalchemy", "psycopg_pool"}),
            Arm.CAPABILITY: cap_runner(set()),
        }
    rep = evaluate(big_plan(arms, counterfactual_policies=[LOW2]))
    return rep.model_copy(update={"capability_id": "pool.abc", "asset_kind": "CAPABILITY"})


def test_decisions_need_a_recorded_counterfactual_and_classify_against_truth() -> None:
    rep = report_for("good")
    (d,) = decisions_from_reports([rep], "A")
    assert (d.family, d.arm, d.governing_policy, d.candidate_policy) == (
        "pool",
        "E",
        "default-low",
        "default-low-v2",
    )
    with pytest.raises(ValueError, match="no candidate-policy counterfactual"):
        decisions_from_reports([rep.model_copy(update={"counterfactuals": []})], "A")
    n = 40
    truth = truth_label(rows_for([i % 3 == 0 for i in range(n)], [True] * n), ASSET, "D")
    assert truth.label == "beneficial"
    forced = AssetDecision(**{**d.__dict__, "governing": Verdict.FAIL, "candidate": Verdict.PASS})
    flags = classify(forced, truth)
    assert flags["governing"]["unjustified_rejection"]
    assert not flags["candidate"]["unjustified_rejection"]
    assert flags["candidate"]["beneficial_promoted"] and not flags["candidate"]["harmful_promotion"]
    assert promoted_by_policy([forced], "candidate") == {"pool": [("E", "pool.abc@1")]}
    assert promoted_by_policy([forced], "governing") == {}


def test_stage_replay_stops_unfit_assets_and_never_stops_a_fit_one() -> None:
    good, bad = report_for("good"), report_for("bad")
    fit = replay_stages(good)
    assert fit.stopped_stage is None and fit.tokens_not_spent == 0
    r = replay_stages(bad, StagePlan())
    assert r.stopped_stage in (2, 3) and r.tokens_not_spent > 0
    assert r.tokens_not_spent < r.tokens_executed and "futility" in " ".join(r.reasons)


def test_asset_cost_reconciles_with_measured_resources() -> None:
    rep = report_for("good")
    assert rep.resources is not None
    c = asset_cost(rep)
    assert c.executed == total_executed_tokens(rep.resources)
    assert c.executed == c.baseline_executed + c.treatment_executed + c.other_executed
    assert c.runs_executed == len(rep.task_results)  # nothing was reused in a fresh evaluation
    with pytest.raises(ValueError, match="no measured resources"):
        asset_cost(rep.model_copy(update={"resources": None}))


def test_the_confirmation_report_is_computed_and_inconclusive_when_data_is_thin() -> None:
    n = 40
    rows = rows_for([i % 3 == 0 for i in range(n)], [True] * n, ref="pool.abc@1", arm="E")
    inputs: list[Any] = [SetInputs("A", [report_for("good"), report_for("bad")], rows, 1000)]
    text = render_confirmation(inputs, "T")
    for needle in (
        "## 1. Asset decisions",
        "## 2. Error counts",
        "## 3. Task-level portfolio",
        "## 4. Mechanical reading",
        "## 5. Sequential rule",
        "SIMULATED",
        "## 6. Measured validation cost",
        "## 7. Lifecycle economics",
        "## 8. Limits",
        "No asset was promoted",
    ):
        assert needle in text, needle
    assert "INCONCLUSIVE" in text.split("## 4.")[1].split("## 5.")[0]
    assert render_confirmation(inputs, "T") == text  # reproducible


def test_standalone_capability_view_counts_fallback_runs_as_capability_failures() -> None:
    from experience_engine.evaluation.policy_confirmation import standalone_capability_rows

    rows = [
        row("E", "t0", 0, True, asset_ref="pool.x@1", fell_back=True),  # saved by planning
        row("E", "t1", 0, True, asset_ref="pool.x@1", fell_back=False),  # capability alone
        row(
            "E", "t2", 0, False, status="UNKNOWN", asset_ref="pool.x@1", fell_back=True
        ),  # UNKNOWN stays missing
        row("D", "t3", 0, True, asset_ref="mem_procedural_pool@1"),  # memory arms untouched
    ]
    got = {r.task_id: (r.verified, r.status) for r in standalone_capability_rows(rows)}
    assert got["t0"] == (False, "FAILURE") and got["t1"] == (True, "VERIFIED_SUCCESS")
    assert got["t2"][1] == "UNKNOWN" and got["t3"][0] is True
    assert rows[0].verified is True  # the recorded rows are not mutated
