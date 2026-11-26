"""M5.10: capability/fallback attribution, UNKNOWN handling, cost accounting, serving-policy
decisions, strategy approval and historical readability."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_capability_runtime import make
from test_experiment import cfg, row, runtime

from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.evaluation import Verdict, default_policies
from experience_engine.domain.experiment import ExperimentResult, RunRow
from experience_engine.domain.serving import (
    Classification,
    ServingPolicy,
    serving_low_v1,
)
from experience_engine.domain.strategy import (
    CapabilityPhase,
    FallbackCause,
    FallbackPhase,
    StrategyExecutionResult,
    fallback_cause,
)
from experience_engine.evaluation import task_stats as ts
from experience_engine.evaluation.experiment_arms import make_runtime_arm
from experience_engine.evaluation.serving_policy import evaluate_assets
from experience_engine.evaluation.serving_report import derive_e1_rows, render_serving_report
from experience_engine.execution.approval import ApprovedCapabilityCatalog, ApprovedMemoryCatalog
from experience_engine.execution.attribution import verified_of
from experience_engine.ports.decisions import DecisionError
from support import FakeClock, first_task

ROOT = Path(__file__).resolve().parents[2]
POLICY = serving_low_v1()
CAP_REF = "pool.0123456789@1"


# ---- attribution through the real production orchestrator -----------------------------------
class Failing:
    def decide(self, **_: Any) -> Any:
        raise DecisionError("model unavailable")


def arm(decisions: Any = None, *, attribute: bool = True, verifier: Any = None) -> Any:
    return make_runtime_arm(
        llm=HeuristicFakeLLM(), config=cfg("E"), verifier=verifier or SimulationVerifier(),
        clock=FakeClock(), runtime=runtime([make()], decisions), attribute=attribute,
    )  # fmt: skip


def test_a_failing_capability_is_attributed_apart_from_the_fallback_that_finishes_it() -> None:
    out = arm(Failing())(first_task())
    s = out.strategy
    assert s is not None and s.strategy_id == "direct-capability" and s.asset_ref == CAP_REF
    assert s.capability is not None and s.capability.reason == "DECISION_FAILED"
    assert s.capability.verified is False and s.capability.outcome_measured  # judged at handover
    assert s.fallback.triggered and s.fallback.attempted
    assert s.fallback.cause == FallbackCause.DECISION_FAILURE
    assert s.fallback.verified is True and s.final_verified is True and out.verified
    # the final success must not be credited to the capability phase
    assert s.capability_verified is False
    cap_tokens, fb_tokens = s.tokens_by_phase
    assert fb_tokens > 0 and s.total_tokens == cap_tokens + fb_tokens == out.tokens
    assert s.phases_consistent() and s.fallback.remaining_tokens is not None


def test_a_successful_capability_has_no_fallback_and_the_final_outcome_is_its_outcome() -> None:
    out = arm()(first_task())
    s = out.strategy
    assert s is not None and s.capability is not None
    assert (s.capability.verified, s.final_verified) == (True, True)
    assert not s.fallback.triggered and s.fallback.tokens == 0
    assert s.total_tokens == s.capability.tokens and s.phases_consistent()


def test_attribution_is_opt_in_and_does_not_change_the_executed_path() -> None:
    plain = arm(Failing(), attribute=False)(first_task())
    attributed = arm(Failing(), attribute=True)(first_task())
    assert plain.strategy is None and attributed.strategy is not None
    assert (plain.status, plain.verified, plain.tokens, plain.tool_calls) == (
        attributed.status, attributed.verified, attributed.tokens, attributed.tool_calls,
    )  # fmt: skip
    assert plain.fell_back == attributed.fell_back and plain.asset_ref == attributed.asset_ref


class Unreadable:
    """A verifier that cannot decide: every call raises."""

    version = "unreadable/1"

    def verify(self, task: Any, snapshot: Any) -> Any:
        raise RuntimeError("verifier down")


def test_unknown_stays_unknown_in_every_phase_and_is_never_a_failure() -> None:
    out = arm(Failing(), verifier=Unreadable())(first_task())
    s = out.strategy
    assert s is not None and s.capability is not None
    assert out.status == "UNKNOWN" and s.final_status == "UNKNOWN"
    assert s.final_verified is None and s.capability.verified is None
    assert s.fallback.verified is None and s.capability.outcome == "UNKNOWN"
    assert verified_of("UNKNOWN") is None and verified_of(None) is None
    assert verified_of("VERIFIED_SUCCESS") is True and verified_of("FAILURE") is False
    assert verified_of("PARTIAL") is False


def test_without_a_handover_verification_the_capability_outcome_is_not_guessed() -> None:
    cap = CapabilityPhase(status="FALLBACK", reason="TOOL_FAILED", outcome_measured=False)
    s = StrategyExecutionResult(
        strategy_id="direct-capability", asset_ref=CAP_REF, capability=cap,
        fallback=FallbackPhase(triggered=True, attempted=True), final_status="VERIFIED_SUCCESS",
        final_verified=True,
    )  # fmt: skip
    assert s.capability_verified is None and s.final_verified is True  # no inference either way


def test_fallback_causes_cover_the_executor_reasons() -> None:
    assert fallback_cause("TOOL_FAILED") == FallbackCause.TOOL_FAILURE
    assert (
        fallback_cause("TIMEOUT") == fallback_cause("MAX_TOKENS") == FallbackCause.TIMEOUT_OR_BUDGET
    )
    assert fallback_cause("VERIFY_FAILED") == FallbackCause.VERIFICATION_FAILURE
    assert fallback_cause("VERIFY_UNKNOWN") == FallbackCause.VERIFICATION_FAILURE
    assert fallback_cause("DECISION_FAILED") == FallbackCause.DECISION_FAILURE
    assert fallback_cause("PRECONDITION_VIOLATED") == FallbackCause.INAPPLICABLE_AT_EXECUTION
    assert fallback_cause("ASSERT_FAILED") == FallbackCause.EXECUTION_FAILURE


# ---- strategy approval / asset isolation -----------------------------------------------------
def test_an_active_but_unapproved_capability_is_never_offered_to_the_runtime() -> None:
    d = make()
    inner = runtime([d]).catalog
    assert ApprovedCapabilityCatalog(inner, [CAP_REF]).list_executable("bench") == [d]
    none = ApprovedCapabilityCatalog(inner, [])
    assert (
        none.list_executable("bench") == []
        and none.get_executable("bench", d.id, d.version) is None
    )
    rt = runtime([d])
    gated = type(rt)(
        catalog=none,
        matcher=type(rt.matcher)(none),
        decisions_for=rt.decisions_for,
        experimental=True,
    )
    out = make_runtime_arm(
        llm=HeuristicFakeLLM(), config=cfg("E"), verifier=SimulationVerifier(),
        clock=FakeClock(), runtime=gated, attribute=True,
    )(first_task())  # fmt: skip
    assert out.asset_ref is None and out.strategy is not None
    assert out.strategy.strategy_id == "planning" and out.strategy.capability is None


def test_approved_memory_catalog_can_only_remove_assets() -> None:
    from test_memory_governance import pool_memory

    from experience_engine.adapters.db.memory_catalog import StaticMemoryCatalog

    m = pool_memory()
    inner = StaticMemoryCatalog("bench", [m])
    assert list(
        ApprovedMemoryCatalog(inner, [f"{m.memory_id}@{m.version}"]).list_active("bench")
    ) == [m]
    assert list(ApprovedMemoryCatalog(inner, ["other@1"]).list_active("bench")) == []
    assert (
        list(
            ApprovedMemoryCatalog(
                StaticMemoryCatalog("bench", []), [f"{m.memory_id}@1"]
            ).list_active("bench")
        )
        == []
    )


# ---- statistics ------------------------------------------------------------------------------
def test_task_level_collapse_uses_strict_majority_and_reports_ties() -> None:
    assert (
        ts.task_binary([True, True, False]) is True
        and ts.task_binary([False, False, True]) is False
    )
    assert ts.task_binary([True, False]) is None  # tie: indeterminate, never evidence
    assert ts.task_binary([None, None]) is None and ts.task_binary([True, None]) is True
    out, ind = ts.collapse_by_task(
        [("a", True), ("a", False), ("b", True), ("b", True), ("c", None)]
    )
    assert out == {"b": True} and ind == 2


def test_exact_bounds_refuse_fractional_counts() -> None:
    assert ts.binom_upper(0, 14, 0.95) < 0.2 < ts.binom_upper(0, 13, 0.95)
    with pytest.raises(ValueError, match="not an integer"):
        ts.binom_upper(14.5, 26, 0.95)  # the M5.9 defect: fractional mass into Clopper-Pearson
    with pytest.raises(ValueError, match="not an integer"):
        ts.binom_lower(3, 26.5, 0.95)
    assert ts.binom_upper(2.0, 26.0, 0.95) == ts.binom_upper(2, 26, 0.95)


def test_evidence_requirement_is_exact_and_never_inflated_by_repetitions() -> None:
    assert ts.required_tasks(0.20, 0.95) == 14 and ts.required_tasks(0.20, 0.95, events=1) == 22
    assert ts.required_tasks(0.20, 0.95, events=9) == 76  # more events need many more tasks
    assert ts.required_tasks(0.20, 0.95, events=9, limit=40) is None  # unreachable within limit
    e = ts.binomial_evidence(0, 9, 0.20, 0.95)
    assert e.needed_total == 14 and e.additional_needed == 5
    assert ts.binomial_evidence(0, 20, 0.20, 0.95).additional_needed == 0


def test_cluster_bootstrap_is_deterministic_and_uses_tasks_as_units() -> None:
    diffs = [1.0, 0.0, 1.0, 1.0, -1.0, 0.0, 1.0, 0.0, 1.0, 1.0]
    a = ts.paired_difference(diffs, 0.95, resamples=2000, seed=3)
    assert a == ts.paired_difference(diffs, 0.95, resamples=2000, seed=3)
    assert a[1] is not None and a[2] is not None and a[1] < a[0] < a[2]  # type: ignore[operator]
    units = [ts.CostUnit(1000, 1, 3000, 1)] * 10
    point, lo, hi, _ = ts.cost_per_success_ratio(units, 0.95, resamples=500, seed=1)
    assert point == pytest.approx(1 / 3) and lo is not None and hi == pytest.approx(1 / 3)
    none = ts.cost_per_success_ratio([ts.CostUnit(1, 0, 1, 1)] * 5, 0.95, resamples=200, seed=1)
    assert none[0] is None  # no verified success: undefined, not zero


def test_stratified_overall_uses_declared_weights_and_stratum_means() -> None:
    est = ts.stratified_mean({"pool": [1.0] * 60, "queue": [0.0] * 30}, resamples=500, seed=1)
    assert est.overall == pytest.approx(2 / 3) and est.by_stratum["pool"] == (60, 1.0)
    eq = ts.stratified_mean(
        {"pool": [1.0] * 60, "queue": [0.0] * 30}, {"pool": 1, "queue": 1}, resamples=500, seed=1
    )
    assert eq.overall == pytest.approx(0.5) and eq.weights == {"pool": 0.5, "queue": 0.5}


def test_wilson_lower_matches_the_known_value() -> None:
    assert ts.wilson_lower(18, 20, 0.95) == pytest.approx(0.738, abs=0.005)
    assert ts.wilson_lower(0, 0) == 0.0


# ---- serving-policy decisions ----------------------------------------------------------------
def cap_row(
    arm: str, task: str, ok: bool, *, family: str = "pool", variant: str = "v",
    tokens: int = 1000, cap_ok: bool | None = None, cap_tokens: int = 200,
    fallback: bool = False, ref: str | None = CAP_REF, status: str | None = None,
    reason: str = "VERIFIED", attempted: bool = True,
) -> RunRow:  # fmt: skip
    strat = None
    if arm == "E" and ref:
        cap = CapabilityPhase(
            status="FALLBACK" if fallback else "COMPLETED", reason=reason,
            outcome="VERIFIED_SUCCESS" if cap_ok else "FAILURE", verified=cap_ok,
            tokens=cap_tokens, tool_calls=2, latency_ms=10,
        )  # fmt: skip
        fb = FallbackPhase(
            triggered=fallback, attempted=fallback, tokens=tokens - cap_tokens if fallback else 0,
            cause=FallbackCause.EXECUTION_FAILURE if fallback else None,
            verified=ok if fallback else None,
        )  # fmt: skip
        strat = StrategyExecutionResult(
            strategy_id="direct-capability", asset_kind="CAPABILITY", asset_ref=ref,
            asset_family=family, applicable=True, attempted=attempted, capability=cap, fallback=fb,
            final_status="VERIFIED_SUCCESS" if ok else "FAILURE", final_verified=ok,
            total_tokens=tokens, budget_tokens=20000,
        )  # fmt: skip
    return row(
        arm, task, 0, ok, family=family, variant=variant, tokens=tokens, status=status,
        asset_ref=ref if arm == "E" else None, asset_kind="CAPABILITY" if arm == "E" else None,
        asset_family=family if arm == "E" else None, strategy=strat, fell_back=fallback,
        capability_used=arm == "E",
    )  # fmt: skip


def grid(n: int, base_ok, cap_ok, final_ok, *, base_tok=6000, tok=1500, fallback=None):  # type: ignore[no-untyped-def]
    rows = []
    for i in range(n):
        t = f"t{i:02d}"
        fb = bool(fallback(i)) if fallback else not cap_ok(i)
        rows.append(cap_row("A", t, base_ok(i), tokens=base_tok, ref=None))
        rows.append(
            cap_row(
                "E",
                t,
                final_ok(i),
                cap_ok=cap_ok(i),
                fallback=fb,
                tokens=tok(i) if callable(tok) else tok,
            )
        )
    return rows


def evaluate(rows: list[RunRow], **kw: Any) -> Any:
    (e,) = evaluate_assets(rows, kw.pop("policy", POLICY), set_name="T", **kw)
    return e


def test_a_good_capability_is_reliable_useful_and_cheaper() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: True, lambda i: True)
    e = evaluate(rows)
    assert (e.safety.verdict, e.intrinsic.verdict, e.serving.verdict, e.economic.verdict) == (
        Verdict.PASS, Verdict.PASS, Verdict.PASS, Verdict.PASS,
    )  # fmt: skip
    assert e.classification == Classification.RELIABLE_AND_USEFUL
    assert e.phases.capability_success_rate == 1.0 and e.phases.fallback_frequency == 0.0
    assert e.in_scope_tasks == 60 and e.policy_name == "serving-low-v1"


def test_fallback_success_is_not_credited_to_the_capability() -> None:
    # capability alone fails everywhere; the planner finishes after the failed attempt
    rows = grid(60, lambda i: i % 3 == 0, lambda i: False, lambda i: True, tok=4000)
    e = evaluate(rows)
    assert (
        e.intrinsic.verdict == Verdict.FAIL and "asset-alone success 0%" in e.intrinsic.reasons[0]
    )
    assert e.serving.verdict == Verdict.PASS  # the strategy as deployed does improve outcomes
    assert e.phases.capability_success_rate == 0.0 and e.phases.fallback_frequency == 1.0
    assert (
        e.phases.recovery_rate == 1.0
        and e.phases.baseline_rate_on_recovery_tasks == pytest.approx(1 / 3)
    )
    assert e.classification in (
        Classification.IMPERFECT_BUT_USEFUL_WITH_FALLBACK, Classification.USEFUL_BUT_COSTLY,
    )  # fmt: skip


def test_a_failing_capability_with_an_expensive_planner_gets_no_efficiency_benefit() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: False, lambda i: True, base_tok=2000, tok=9000)
    e = evaluate(rows)
    assert e.serving.verdict == Verdict.PASS  # success improved...
    assert e.economic.verdict == Verdict.FAIL  # ...but each verified success costs more
    assert e.classification == Classification.USEFUL_BUT_COSTLY
    assert all(r.break_even_successes is None for r in e.economics)  # execution never recovers it


def test_a_reliable_capability_that_costs_more_per_success_is_not_worth_its_runtime_cost() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: True, lambda i: True, base_tok=500, tok=3000)
    e = evaluate(rows)
    assert e.intrinsic.verdict == Verdict.PASS and e.serving.verdict == Verdict.PASS
    assert e.economic.verdict == Verdict.FAIL
    assert e.classification == Classification.RELIABLE_NOT_WORTH_COST


def test_a_strategy_that_loses_to_the_baseline_is_harmful_and_savings_cannot_rescue_it() -> None:
    rows = grid(
        60,
        lambda i: i % 2 == 0,
        lambda i: i % 5 == 0,
        lambda i: i % 5 == 0,
        tok=300,
        fallback=lambda i: False,
    )
    e = evaluate(rows)
    assert e.serving.verdict == Verdict.FAIL
    assert e.classification == Classification.HARMFUL
    assert e.economic.verdict != Verdict.PASS  # cheaper tokens never count when serving fails


def test_too_few_tasks_is_inconclusive_with_an_exact_evidence_requirement() -> None:
    rows = grid(12, lambda i: i % 3 == 0, lambda i: True, lambda i: True)
    e = evaluate(rows)
    assert (
        e.serving.verdict == Verdict.INCONCLUSIVE
        and e.classification == Classification.INSUFFICIENT
    )
    assert "8 more independent in-scope tasks" in (e.serving.requirement or "")
    assert "more independent attempted tasks" in (e.intrinsic.requirement or "")


def test_too_few_baseline_solved_tasks_asks_for_more_tasks_without_weakening_the_threshold() -> (
    None
):
    # 60 tasks but the baseline solves only 6: regression risk cannot be bounded
    rows = grid(60, lambda i: i < 6, lambda i: True, lambda i: True)
    e = evaluate(rows)
    assert e.serving.verdict == Verdict.INCONCLUSIVE
    assert "more independent baseline-solved tasks" in (e.serving.requirement or "")
    assert POLICY.max_regression_rate_ucb == 0.20 and POLICY.min_baseline_solved_tasks == 14


def test_safety_violations_are_zero_tolerance() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: True, lambda i: True)
    bad = [
        r.model_copy(update={"termination": "TOOL_NOT_ALLOWED"})
        if r.task_id == "t01" and r.arm == "E"
        else r
        for r in rows
    ]
    e = evaluate(bad)
    assert e.safety.verdict == Verdict.FAIL and e.safety.critical_violations == 1
    assert e.classification == Classification.HARMFUL and e.economic.verdict != Verdict.PASS
    # false application: the asset served a task outside its declared family
    stray = cap_row("E", "x01", True, family="queue")
    stray = stray.model_copy(update={"asset_ref": CAP_REF, "asset_family": "pool"})
    f = evaluate([*rows, cap_row("A", "x01", False, family="queue", ref=None), stray])
    assert f.safety.false_applications == 1 and f.safety.verdict == Verdict.FAIL
    # untested constraints are listed, never silently passed
    assert any("cross-tenant" in u for u in f.safety.untested)


def test_budget_overshoot_is_a_safety_failure() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: True, lambda i: True)
    s0 = rows[1].strategy
    assert s0 is not None
    over = rows[1].model_copy(update={"strategy": s0.model_copy(update={"total_tokens": 30000})})
    e = evaluate([rows[0], over, *rows[2:]])
    assert e.safety.budget_violations == 1 and e.safety.verdict == Verdict.FAIL


def test_unknown_runs_are_missing_data_not_failures() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: True, lambda i: True)
    unk = [
        cap_row("E", r.task_id, False, status="UNKNOWN", cap_ok=None, ref=CAP_REF)
        if (r.arm == "E" and r.task_id < "t05")
        else r
        for r in rows
    ]
    e = evaluate(unk)
    assert e.in_scope_tasks == 55 and e.phases.unknown_runs == 5
    assert e.serving.verdict == Verdict.PASS  # five unknowns did not become five failures


def test_decisions_are_reproducible_and_carry_the_policy_hash() -> None:
    rows = grid(60, lambda i: i % 3 == 0, lambda i: True, lambda i: True)
    assert evaluate(rows) == evaluate(rows)
    assert evaluate(rows).policy_hash == POLICY.policy_hash()


def test_serving_policy_is_versioned_frozen_and_separate_from_the_promotion_policies() -> None:
    p = serving_low_v1()
    assert (p.name, p.version, p.max_regression_rate_ucb, p.min_baseline_solved_tasks) == (
        "serving-low-v1",
        1,
        0.20,
        14,
    )
    assert p.policy_hash() == PINNED_SERVING_HASH
    assert default_policies()["low"].policy_hash().startswith("7b6f0944819b")  # unchanged
    assert not isinstance(p, type(default_policies()["low"]))
    with pytest.raises(ValidationError):
        ServingPolicy.model_validate({**p.model_dump(), "extra": 1})
    assert p.evaluation_config_hash({"seeds": [0]}) != p.evaluation_config_hash({"seeds": [1]})


PINNED_SERVING_HASH = "59c4bd37dc79a1c8a9c074bbc3309ccbf606dad9020ef47ba0f14378982fcb52"


# ---- E1 derivation and historical readability ------------------------------------------------
def test_e1_is_the_capability_phase_of_the_same_runs() -> None:
    rows = grid(10, lambda i: i % 2 == 0, lambda i: i < 4, lambda i: True, tok=2000)
    e1 = derive_e1_rows(rows)
    assert len(e1) == 10 and {r.arm for r in e1} == {"E1"}
    by = {r.task_id: r for r in e1}
    assert by["t00"].verified and not by["t09"].verified
    assert by["t00"].tokens == 200 and by["t00"].strategy is None
    assert all(r.status in ("VERIFIED_SUCCESS", "FAILURE") for r in e1)
    unk = cap_row("E", "u", False, cap_ok=None, ref=CAP_REF)
    assert derive_e1_rows([unk])[0].status == "UNKNOWN"
    assert derive_e1_rows([cap_row("A", "a", True, ref=None)]) == []  # only attempted E runs


def test_old_rows_and_results_without_attribution_still_load_and_render() -> None:
    old = row("A", "t0", 0, True).model_dump_json()
    loaded = RunRow.model_validate_json(old)
    assert loaded.strategy is None
    path = ROOT / "docs/evaluations/data/gated-production-gemini-2.5-flash.json"
    res = ExperimentResult.model_validate_json(path.read_text())
    text, evals = render_serving_report([("M58", res)], title="T")
    assert evals == [] and "phase attribution recorded: **no**" in text
    again = ExperimentResult.model_validate_json(res.model_dump_json())
    assert again == res  # round trip is lossless


def test_new_rows_round_trip_with_their_attribution() -> None:
    rows = grid(4, lambda i: True, lambda i: False, lambda i: True)
    r = rows[1]
    again = RunRow.model_validate_json(r.model_dump_json())
    assert again == r and again.strategy is not None and again.strategy.fallback.triggered
    assert json.loads(r.model_dump_json())["strategy"]["capability"]["verified"] is False


def test_the_report_renders_decisions_attribution_and_separates_the_layers() -> None:
    from test_experiment import synthetic_result

    base = synthetic_result()
    rows = grid(60, lambda i: i % 3 == 0, lambda i: False, lambda i: True, tok=4000)
    res = base.model_copy(update={"rows": rows})
    text, evals = render_serving_report([("S", res)], title="T")
    assert len(evals) == 1
    for needle in (
        "## Set S", "E1 capability only (derived)", "E2 capability + production fallback",
        "### Capability / fallback attribution", "## Asset decisions", "Intrinsic (Layer A)",
        "Serving improves (Layer B)", "## Lifecycle economics", "no asset was promoted",
        "Stratified production estimate",
    ):  # fmt: skip
        assert needle in text, needle
    assert render_serving_report([("S", res)], title="T")[0] == text  # reproducible
