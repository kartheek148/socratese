"""M5.9: risk-based promotion policy (`*-v2`), sequential stopping, baseline cache, resource
accounting and the retrospective simulation. The existing count-based policy must stay unchanged."""

import json
import threading
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from test_evaluation_engine import ALL, KNOWN, base_runner, cap_runner, out
from test_evaluation_engine import plan as base_plan

from experience_engine.bench.dataset import DEFAULT_SEED, BenchTask, generate_split
from experience_engine.domain.benchmark import Split
from experience_engine.domain.evaluation import (
    AnyPolicy,
    Arm,
    PromotionPolicy,
    RiskBasedPolicy,
    RiskEvidence,
    Suite,
    TaskResult,
    Verdict,
    default_policies,
    default_policies_v2,
)
from experience_engine.evaluation.arms import CURRENT_SEED, ArmOutcome, SharedArm
from experience_engine.evaluation.baseline_cache import (
    BaselineIdentity,
    BaselineStore,
    task_fingerprint,
)
from experience_engine.evaluation.engine import evaluate
from experience_engine.evaluation.paired import holm
from experience_engine.evaluation.policy import POLICY_SETS, builtin_policies, load_policies
from experience_engine.evaluation.policy_simulation import (
    aggregates_from_dir,
    load_aggregates,
    replay_v1,
    replay_v2,
)
from experience_engine.evaluation.risk import (
    build_risk_evidence,
    cp_lower,
    cp_upper,
    paired_bootstrap_bounds,
    sign_test_worse,
)
from experience_engine.evaluation.risk_policy import critical_violations, decide_verdict_v2
from experience_engine.evaluation.sequential import RunPair, StagePlan, stop_reasons
from experience_engine.evaluation.validation_cost import (
    resources_from_rows,
    total_cached_tokens,
    total_executed_tokens,
)

ROOT = Path(__file__).resolve().parents[2]
V1_LOW_HASH = "7b6f0944819b6d81a5a5caa29b8f23837b8aff83a1be6531cb99c73ae9856bf9"
LOW2 = default_policies_v2()["low"]
MED2 = default_policies_v2()["medium"]
HIGH2 = default_policies_v2()["high"]
T = Arm.MEMORY_ASSET


# ---- the existing policy is untouched -------------------------------------------------------
def test_existing_default_low_is_bit_for_bit_unchanged() -> None:
    low = default_policies()["low"]
    assert low.policy_hash() == V1_LOW_HASH
    assert (low.name, low.version, low.max_regressions) == ("default-low", 1, 1)
    assert "method" not in low.model_dump()  # v2-only fields never leak into v1
    assert not isinstance(low, RiskBasedPolicy)


def test_m58_decisions_are_reproduced_exactly_under_the_existing_policy() -> None:
    aggs = aggregates_from_dir(ROOT / "docs/evaluations/gated-validation")
    assert len(aggs) == 8
    for a in aggs:
        verdict, reasons = replay_v1(a)
        assert verdict.value == a.recorded_verdict and reasons == a.recorded_reasons, a.asset
    stored = load_aggregates(ROOT / "docs/evaluations/data/m58-validation-aggregates.json")
    assert stored == aggs  # the committed aggregates are what the reports parse to


# ---- statistics -----------------------------------------------------------------------------
def test_clopper_pearson_matches_reference_values() -> None:
    assert cp_upper(0, 16, 0.95) == pytest.approx(0.1710, abs=1e-3)
    assert cp_upper(3, 27, 0.95) == pytest.approx(0.2630, abs=1e-3)
    assert cp_upper(20, 27, 0.95) == pytest.approx(0.8710, abs=1e-3)
    assert cp_lower(3, 27, 0.95) == pytest.approx(0.0310, abs=1e-3)
    assert cp_upper(0, 14, 0.95) < 0.2 < cp_upper(0, 13, 0.95)  # >= 14 clean tasks for delta=.2
    assert cp_upper(0, 29, 0.95) < 0.1 < cp_upper(0, 28, 0.95)
    assert cp_upper(0, 59, 0.95) < 0.05 < cp_upper(0, 58, 0.95)


def test_bounds_edges_and_real_valued_counts() -> None:
    assert cp_upper(5, 5, 0.95) == 1.0 and cp_upper(0, 0, 0.95) == 1.0
    assert cp_lower(0, 10, 0.95) == 0.0
    assert cp_upper(1.5, 13.5, 0.95) == pytest.approx(0.353, abs=2e-3)
    assert cp_upper(2, 20, 0.95) > cp_upper(1, 20, 0.95) > cp_upper(0, 20, 0.95)
    assert cp_upper(1, 40, 0.95) < cp_upper(1, 20, 0.95)  # more evidence narrows the bound


def test_sign_test_and_bootstrap_are_exact_and_deterministic() -> None:
    assert sign_test_worse(5, 0) == pytest.approx(1 / 32)
    assert sign_test_worse(0, 0) == 1.0 and sign_test_worse(0, 4) == 1.0
    diffs = [0.0, 1.0, 0.5, -0.5, 1.0, 0.0, 0.5, 1.0]
    a = paired_bootstrap_bounds(diffs, 0.95, resamples=2000, seed=7)
    assert a == paired_bootstrap_bounds(diffs, 0.95, resamples=2000, seed=7)
    assert a[1] < a[0] < a[2]
    assert paired_bootstrap_bounds([0.2, 0.2], 0.95, resamples=1000, seed=1) == (0.2, 0.2, 0.2)


# ---- task-level evidence --------------------------------------------------------------------
def row(suite: Suite, arm: Arm, task: str, ok: bool | None, *, seed: int = 0, variant: str = "v",
        termination: str | None = None) -> TaskResult:  # fmt: skip
    status = "UNKNOWN" if ok is None else ("VERIFIED_SUCCESS" if ok else "FAILURE")
    return TaskResult(
        suite=suite, arm=arm, task_id=task, family="pool", variant=variant, status=status,
        tokens=100, tool_calls=3, latency_ms=10, termination=termination, seed=seed,
    )  # fmt: skip


def pair(suite: Suite, task: str, base: bool | None, treat: bool | None, **kw) -> list[TaskResult]:  # type: ignore[no-untyped-def]
    return [row(suite, Arm.BASELINE, task, base, **kw), row(suite, T, task, treat, **kw)]


def test_evidence_counts_regressions_at_the_task_level_and_drops_unknown() -> None:
    H = Suite.HELD_OUT
    rows = [
        *pair(H, "a", True, False, seed=0), *pair(H, "a", True, True, seed=1),  # regressed 1 of 2
        *pair(H, "b", True, True), *pair(H, "c", False, True),  # solved; improved
        *pair(H, "d", None, False), *pair(H, "e", True, None),  # missing data: dropped
        *pair(Suite.SOURCE_REPLAY, "s1", True, True),
        *pair(Suite.SOURCE_REPLAY, "s2", False, False),
        *pair(Suite.TRANSFER, "t", True, False, variant="w", termination="TOOL_NOT_ALLOWED"),
    ]  # fmt: skip
    ev = build_risk_evidence(rows, T.value)
    assert (ev.n_pairs, ev.n_regressions, ev.n_dropped_unknown) == (5, 2, 2)
    assert ev.regression_tasks == 4  # a, b, t and s1: tasks the baseline solved at least once
    assert ev.regression_mass == pytest.approx(0.5 + 0 + 1 + 0)  # a regressed in half its runs
    assert ev.improvement_tasks == 1
    assert ev.variant_pairs == {"v": [4, 1], "w": [1, 1]}
    assert ev.source_tasks == 2 and ev.source_mass == 1.0
    assert sorted(ev.paired_diffs) == [-1.0, -0.5, 0.0, 1.0]  # held-out + transfer tasks only
    assert ev.termination_counts == {"TOOL_NOT_ALLOWED": 1}


# ---- v2 verdict -----------------------------------------------------------------------------
def suites(held=(36, 34), transfer=(36, 33), source=(16, 14), n_tasks=18):  # type: ignore[no-untyped-def]
    from reports import arm_metrics

    def m(n_ok, tasks):  # type: ignore[no-untyped-def]
        return {T.value: arm_metrics(*n_ok).model_copy(update={"n_tasks": tasks})}

    return {
        "HELD_OUT": m(held, n_tasks),
        "TRANSFER": m(transfer, n_tasks),
        "SOURCE_REPLAY": m(source, 8),
        "NEGATIVE": m((8, 8), 8),
    }


def evidence(
    tasks: int, mass: float, *, diffs=None, variants=None, term=None, src=(8, 7.0)
) -> RiskEvidence:  # type: ignore[no-untyped-def]
    return RiskEvidence(
        n_pairs=tasks, n_regressions=round(mass), n_dropped_unknown=0, regression_tasks=tasks,
        regression_mass=mass, improvement_tasks=10, variant_pairs=variants or {},
        paired_diffs=[0.5] * 18 if diffs is None else diffs, source_tasks=src[0],
        source_mass=src[1], termination_counts=term or {},
    )  # fmt: skip


def verdict(ev: RiskEvidence, policy: RiskBasedPolicy = LOW2, **kw):  # type: ignore[no-untyped-def]
    s = kw.pop("suites", None) or suites()
    return decide_verdict_v2(
        policy, s, false_applicability_rate=0.0, evidence=ev, treatment=T, **kw
    )


def test_passes_when_every_gate_holds_and_cannot_pass_on_thin_evidence() -> None:
    assert verdict(evidence(40, 2.0))[0] == Verdict.PASS  # 5% rate, UCB < 20%
    v, why = verdict(evidence(8, 0.0))  # 0/8: UCB 31% > 20%: not enough evidence
    assert v == Verdict.INCONCLUSIVE and "more evidence needed" in why[0]
    v, why = verdict(evidence(7, 0.0))
    assert v == Verdict.INCONCLUSIVE


def test_regression_gate_is_three_valued_and_monotone_in_evidence() -> None:
    assert verdict(evidence(20, 4.0))[0] == Verdict.INCONCLUSIVE  # 20%, wide interval
    assert verdict(evidence(20, 10.0))[0] == Verdict.FAIL  # 50%: LCB above 20%
    # same 4% rate: small samples cannot show it, large ones can; volume never flips it to FAIL
    outcomes = [verdict(evidence(n, 0.04 * n))[0] for n in (10, 25, 50, 100, 200, 400)]
    assert outcomes[0] == Verdict.INCONCLUSIVE and outcomes[-1] == Verdict.PASS
    assert Verdict.FAIL not in outcomes
    first_pass = outcomes.index(Verdict.PASS)
    assert all(o == Verdict.PASS for o in outcomes[first_pass:])


def test_the_existing_count_rule_rejects_the_same_good_asset_more_often_with_more_data() -> None:
    from math import comb

    def reject(n: int, p: float) -> float:  # P(>= 2 regressions): the existing max_regressions=1
        return 1 - sum(comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(2))

    assert reject(27, 0.03) < reject(100, 0.03) < reject(200, 0.03)  # worse with more evidence
    # the risk-based rule at the same 3% rate does not reject at any volume
    assert all(verdict(evidence(n, 0.03 * n))[0] != Verdict.FAIL for n in (27, 100, 200))


def test_thresholds_depend_on_the_risk_class() -> None:
    ev = evidence(60, 4.0)  # 6.7% observed, UCB ~14%
    assert verdict(ev, LOW2)[0] == Verdict.PASS
    assert verdict(ev, MED2)[0] != Verdict.PASS  # delta 10%
    assert verdict(evidence(60, 0.0), MED2, suites=suites((36, 36), (36, 36), (16, 16)))[0] in (
        Verdict.PASS,
        Verdict.INCONCLUSIVE,
    )
    assert (LOW2.max_regression_rate_ucb, MED2.max_regression_rate_ucb) == (0.20, 0.10)
    assert HIGH2.max_regression_rate_ucb == 0.05 and HIGH2.noninferiority_margin == 0.0
    assert HIGH2.min_regression_tasks > MED2.min_regression_tasks > LOW2.min_regression_tasks


def test_zero_tolerance_for_critical_violations_in_every_risk_class() -> None:
    ev = evidence(60, 0.0, term={"TOOL_NOT_ALLOWED": 1, "COMPLETED:VERIFIED": 80})
    for pol in (LOW2, MED2, HIGH2):
        assert critical_violations(pol, ev) == 1
        v, why = verdict(ev, pol)
        assert v == Verdict.FAIL and "critical violation" in why[0]
    assert critical_violations(LOW2, evidence(60, 0.0, term={"TOOL_NOT_ALLOWED:x": 2})) == 2


def test_improvements_cannot_conceal_a_regressing_variant() -> None:
    fine = evidence(60, 3.0, variants={"easy": [40, 1], "hard": [8, 1]})
    assert verdict(fine)[0] == Verdict.PASS
    hidden = evidence(60, 3.0, variants={"easy": [40, 0], "hard": [8, 5]})
    v, why = verdict(hidden)
    assert v == Verdict.FAIL and any("variant hard" in w for w in why)
    tiny = evidence(60, 3.0, variants={"rare": [3, 3]})  # below min_variant_pairs: not judged
    assert verdict(tiny)[0] == Verdict.PASS


def test_non_inferiority_is_a_task_level_paired_gate() -> None:
    assert verdict(evidence(40, 2.0, diffs=[0.5] * 18))[0] == Verdict.PASS
    worse = verdict(evidence(40, 2.0, diffs=[-0.5] * 18))
    assert worse[0] == Verdict.FAIL and "worse than baseline" in worse[1][0]
    noisy = [1.0, -1.0] * 4  # mean 0 over 8 tasks: cannot show non-inferiority
    assert verdict(evidence(40, 2.0, diffs=noisy))[0] == Verdict.INCONCLUSIVE
    assert verdict(evidence(40, 2.0, diffs=[]))[0] == Verdict.INCONCLUSIVE
    assert verdict(evidence(40, 2.0, diffs=[]), require_paired=False)[0] == Verdict.PASS


def test_source_replay_fails_only_when_the_evidence_excludes_the_requirement() -> None:
    assert verdict(evidence(40, 2.0, src=(8, 6.0)))[0] == Verdict.PASS  # 75% on 8 tasks: UCB > 80%
    v, why = verdict(evidence(40, 2.0, src=(8, 2.0)))  # 25%
    assert v == Verdict.FAIL and "excludes the required 80%" in why[0]
    assert verdict(evidence(40, 2.0, src=(0, 0.0)))[0] == Verdict.INCONCLUSIVE


def test_success_applicability_and_sample_gates_are_kept() -> None:
    ev = evidence(40, 2.0)
    assert verdict(ev, suites=suites(held=(36, 20)))[0] == Verdict.FAIL
    assert verdict(ev, suites=suites(transfer=(36, 20)))[0] == Verdict.FAIL
    s = suites()
    v, why = decide_verdict_v2(LOW2, s, false_applicability_rate=0.25, evidence=ev, treatment=T)
    assert v == Verdict.FAIL and "false applicability" in why[0]
    assert verdict(ev, suites=suites(n_tasks=2))[0] == Verdict.INCONCLUSIVE  # too few tasks


def test_token_savings_can_never_buy_back_a_failure() -> None:
    bad = evidence(20, 10.0)
    cheap = suites()
    assert verdict(bad, suites=cheap)[0] == Verdict.FAIL  # no cost term exists in v2 gates
    assert "token" not in " ".join(verdict(bad, suites=cheap)[1]).lower()


def test_legacy_max_regressions_field_is_not_read_by_v2() -> None:
    other = RiskBasedPolicy.model_validate({**LOW2.model_dump(), "max_regressions": 0})
    ev = evidence(40, 2.0)
    assert verdict(ev, other) == verdict(ev, LOW2)


# ---- policy versioning ----------------------------------------------------------------------
def test_policies_are_independently_versioned_and_hash_distinct() -> None:
    v1, v2 = default_policies(), default_policies_v2()
    assert [p.name for p in v2.values()] == [
        "default-low-v2",
        "default-medium-v2",
        "default-high-v2",
    ]
    assert all(p.version == 2 for p in v2.values())
    hashes = {p.policy_hash() for p in (*v1.values(), *v2.values())}
    assert len(hashes) == 6
    for risk in v1:  # shared fields equal the v1 values of the same risk class
        a, b = v1[risk].model_dump(), v2[risk].model_dump()
        for k in ("min_heldout_success_rate", "min_transfer_success_rate",
                  "max_false_applicability_rate", "min_success_lower_bound", "min_heldout_n",
                  "min_shadow_observations", "min_canary_observations"):  # fmt: skip
            assert a[k] == b[k], (risk, k)
    required = {"method", "confidence", "max_regression_rate_ucb", "min_regression_tasks",
                "noninferiority_margin", "critical_terminations", "bootstrap_seed"}  # fmt: skip
    assert required <= set(LOW2.model_dump())


def test_v1_and_v2_json_never_cross_validate_and_round_trip() -> None:
    v1j, v2j = default_policies()["low"].model_dump_json(), LOW2.model_dump_json()
    with pytest.raises(ValidationError):
        RiskBasedPolicy.model_validate_json(v1j)
    with pytest.raises(ValidationError):
        PromotionPolicy.model_validate_json(v2j)
    ta: TypeAdapter[AnyPolicy] = TypeAdapter(AnyPolicy)
    assert ta.validate_json(v2j) == LOW2 and ta.validate_json(v1j) == default_policies()["low"]
    assert ta.validate_json(v2j).policy_hash() == LOW2.policy_hash()


def test_policy_sets_and_policy_files(tmp_path: Path) -> None:
    assert POLICY_SETS == ("default", "risk-v2")
    assert load_policies(None)["low"].policy_hash() == V1_LOW_HASH
    assert isinstance(load_policies(None, "risk-v2")["low"], RiskBasedPolicy)
    with pytest.raises(ValueError, match="unknown policy set"):
        builtin_policies("nope")
    f = tmp_path / "p.json"
    f.write_text(
        json.dumps({k: v.model_dump(mode="json") for k, v in default_policies_v2().items()})
    )
    assert load_policies(f)["medium"] == default_policies_v2()["medium"]


def test_multiple_comparison_budget_is_explicit() -> None:
    plan = StagePlan()
    assert plan.total_alpha == pytest.approx(0.03) and plan.total_alpha < 0.05
    adj = holm({"a": 0.01, "b": 0.02, "c": 0.04})
    assert adj == {"a": 0.03, "b": 0.04, "c": 0.04}  # Holm for the confirmation's comparisons


# ---- sequential stopping --------------------------------------------------------------------
def pairs(spec: list[tuple[str, bool, bool]]) -> list[RunPair]:
    return [RunPair(t, b, c) for t, b, c in spec]


def test_stopping_rules_reject_only_when_the_data_exclude_fitness() -> None:
    bad = pairs([(f"t{i}", True, False) for i in range(8)])
    why = stop_reasons(bad, alpha=0.01, required_success_rate=0.75, stage=2)
    assert any("futility" in w for w in why) and any("harm" in w for w in why)
    assert all(w.startswith("stage 2:") for w in why)
    good = pairs([(f"t{i}", i % 2 == 0, True) for i in range(8)])
    assert stop_reasons(good, alpha=0.01, required_success_rate=0.75, stage=2) == []
    borderline = pairs([(f"t{i}", True, i < 6) for i in range(8)])  # 75% observed: do not stop
    assert stop_reasons(borderline, alpha=0.01, required_success_rate=0.75, stage=2) == []
    assert stop_reasons([], alpha=0.01, required_success_rate=0.75, stage=2) == []


def test_stopping_is_task_level_repeats_do_not_inflate_the_evidence() -> None:
    # one task failing in 8 repetitions is one observation, not eight
    one_task = pairs([("t0", True, False)] * 8)
    assert stop_reasons(one_task, alpha=0.01, required_success_rate=0.75, stage=3) == []


# ---- engine: counterfactuals, staging, resources --------------------------------------------
def clone(tasks, k):  # type: ignore[no-untyped-def]
    out_: list[BenchTask] = []
    for i in range(k):
        for t in tasks:
            out_.append(BenchTask(
                task=t.task.model_copy(update={"task_id": f"{t.task.task_id}#{i}"}),
                environment=t.environment, family=t.family, variant=t.variant,
            ))  # fmt: skip
    return out_


def big_plan(arms, **kw):  # type: ignore[no-untyped-def]
    p = base_plan(arms, **kw)
    p.source_tasks = clone([t for t in p.source_tasks if t.family == "pool"], 2)
    p.heldout_tasks = clone([t for t in p.heldout_tasks if t.family == "pool"], 12)
    p.transfer_tasks = clone([t for t in p.transfer_tasks if t.family == "pool"], 12)
    return p


def good_arms():  # type: ignore[no-untyped-def]
    return {
        Arm.BASELINE: base_runner(KNOWN, tokens=1000),
        Arm.CAPABILITY: cap_runner(ALL, tokens=100),
    }


def test_counterfactual_verdicts_are_recorded_but_never_decide() -> None:
    rep = evaluate(big_plan(good_arms(), counterfactual_policies=[LOW2]))
    assert rep.policy_name == "default-low" and rep.verdict == Verdict.PASS
    (cf,) = rep.counterfactuals
    assert cf.policy_name == "default-low-v2" and cf.policy_hash == LOW2.policy_hash()
    alone = evaluate(big_plan(good_arms()))
    assert alone.verdict == rep.verdict and alone.reasons == rep.reasons  # unaffected
    assert alone.counterfactuals == []
    assert rep.evaluation_config_hash and rep.evaluation_config_hash == alone.evaluation_config_hash


def test_a_v2_governing_policy_produces_a_report_that_round_trips() -> None:
    from experience_engine.domain.evaluation import EvaluationReport

    rep = evaluate(
        big_plan(good_arms(), policy=LOW2, counterfactual_policies=[default_policies()["low"]])
    )
    assert isinstance(rep.policy, RiskBasedPolicy) and rep.policy_hash == LOW2.policy_hash()
    assert rep.risk_evidence is not None and rep.risk_evidence.n_pairs > 0
    again = EvaluationReport.model_validate_json(rep.model_dump_json())
    assert again == rep and isinstance(again.policy, RiskBasedPolicy)


def test_a_config_hash_changes_with_policy_seeds_and_stages() -> None:
    a = evaluate(big_plan(good_arms())).evaluation_config_hash
    assert a != evaluate(big_plan(good_arms(), policy=LOW2)).evaluation_config_hash
    assert a != evaluate(big_plan(good_arms(), seeds=[0, 1])).evaluation_config_hash
    assert a != evaluate(big_plan(good_arms(), stages=StagePlan())).evaluation_config_hash


def test_staged_run_on_a_fit_asset_reaches_the_final_stage_and_matches_the_full_verdict() -> None:
    full = evaluate(big_plan(good_arms(), policy=LOW2))
    staged = evaluate(big_plan(good_arms(), policy=LOW2, stages=StagePlan()))
    assert (staged.stage_reached, staged.early_stopped) == (4, False)
    assert staged.verdict == full.verdict and staged.reasons == full.reasons
    assert set(staged.suites) == set(full.suites)


def test_staged_run_stops_a_clearly_unfit_asset_early_and_spends_less() -> None:
    arms = {Arm.BASELINE: base_runner(KNOWN), Arm.CAPABILITY: cap_runner(set())}  # never solves
    full = evaluate(big_plan(arms, policy=LOW2))
    staged = evaluate(big_plan(arms, policy=LOW2, stages=StagePlan()))
    assert staged.early_stopped and staged.verdict == Verdict.FAIL
    assert staged.stage_reached in (2, 3) and staged.reasons[0].startswith("EARLY STOP stage")
    assert full.verdict == Verdict.FAIL  # the full evaluation agrees
    assert len(staged.task_results) < len(full.task_results)
    assert "TRANSFER" not in staged.suites  # the later suites were never executed
    assert staged.resources is not None and staged.resources.early_stopped


def test_staging_never_passes_early_and_stage_one_is_free() -> None:
    def refuses_nothing(task):  # type: ignore[no-untyped-def]
        return out(True, 100, applicable=True)  # applies everywhere: false applicability

    arms = {Arm.BASELINE: base_runner(KNOWN), Arm.CAPABILITY: refuses_nothing}
    rep = evaluate(big_plan(arms, policy=LOW2, stages=StagePlan()))
    assert rep.early_stopped and rep.stage_reached == 1 and rep.verdict == Verdict.FAIL
    assert "false applicability" in rep.reasons[0]
    assert all(r.suite == Suite.NEGATIVE for r in rep.task_results)  # no suite run was started
    # a great asset is never decided PASS before the final stage
    ok = evaluate(big_plan(good_arms(), policy=LOW2, stages=StagePlan()))
    assert ok.stage_reached == 4 and not ok.early_stopped


def test_resources_reconcile_with_the_task_rows_and_separate_cached_runs() -> None:
    shared = SharedArm(base_runner(KNOWN, tokens=1000))
    arms = {Arm.BASELINE: shared, Arm.CAPABILITY: cap_runner(ALL, tokens=100)}
    first = evaluate(big_plan(arms))
    second = evaluate(big_plan(arms))  # same session: the baseline is served, not re-run
    r1, r2 = first.resources, second.resources
    assert r1 is not None and r2 is not None
    assert r1.by_arm["A_BASELINE"].runs_cached == 0 and r1.by_arm["A_BASELINE"].runs_executed > 0
    assert r2.by_arm["A_BASELINE"].runs_executed == 0 and r2.by_arm["A_BASELINE"].runs_cached > 0
    assert total_executed_tokens(r1) == sum(x.tokens for x in first.task_results if not x.cache_hit)
    assert (
        total_cached_tokens(r2)
        == total_executed_tokens(r1) - r1.by_arm["C_CAPABILITY"].tokens_executed
    )
    assert shared.computed == r1.by_arm["A_BASELINE"].runs_executed
    # the report still *charges* reuse in the lifecycle figure (as M5.8 did), but only the
    # executed tokens are real spend: the two must stay distinguishable
    assert second.lifecycle.evaluation_overhead_tokens > 0
    assert (
        total_executed_tokens(r2)
        < second.lifecycle.evaluation_overhead_tokens + r2.by_arm["C_CAPABILITY"].tokens_executed
    )


def test_prompt_and_completion_tokens_are_summed_only_where_the_split_is_known() -> None:
    rows = [
        TaskResult(suite=Suite.HELD_OUT, arm=Arm.BASELINE, task_id="a", family="f", variant="v",
                   status="FAILURE", tokens=100, tool_calls=1, latency_ms=5,
                   prompt_tokens=70, completion_tokens=30),
        TaskResult(suite=Suite.HELD_OUT, arm=Arm.CAPABILITY, task_id="a", family="f", variant="v",
                   status="FAILURE", tokens=40, tool_calls=1, latency_ms=5),
    ]  # fmt: skip
    res = resources_from_rows(rows)
    a, c = res.by_arm["A_BASELINE"], res.by_arm["C_CAPABILITY"]
    assert (a.prompt_tokens, a.completion_tokens, a.runs_with_split) == (70, 30, 1)
    assert (c.prompt_tokens, c.runs_with_split, c.tokens_executed) == (0, 0, 40)


# ---- baseline cache -------------------------------------------------------------------------
IDENT = BaselineIdentity(
    model_settings={"provider": "p", "model": "m"}, prompt_version="p1", runtime_version="r1",
    verifier_version="v1", environment_version="e1", agent_config_id="baseline-agent",
)  # fmt: skip


def a_task(first: int = 0) -> BenchTask:
    return generate_split(Split.VALIDATION, DEFAULT_SEED)[first]


def test_cache_hits_only_when_every_component_matches(tmp_path: Path) -> None:
    from dataclasses import replace

    t = a_task()
    store = BaselineStore(tmp_path, IDENT)
    k = store.key(t, 0, "dsA")
    assert store.get(k) is None
    store.put(k, out(True, 321))
    got = store.get(k)
    assert got is not None and got.tokens == 321 and store.hits == 1 and store.misses == 1
    assert store.tokens_avoided == 321 and store.hit_rate == 0.5
    same = BaselineStore(tmp_path, IDENT)  # a new process / session sees the same entry
    assert same.get(same.key(t, 0, "dsA")) is not None
    variants = {
        "dataset": store.key(t, 0, "dsB"),
        "repetition": store.key(t, 1, "dsA"),
        "task": store.key(a_task(1), 0, "dsA"),
    }
    for name, kk in variants.items():
        assert kk != k and store.get(kk) is None, name
    for fld, val in (
        ("model_settings", {"provider": "p", "model": "m2"}),
        ("prompt_version", "p2"),
        ("runtime_version", "r2"),
        ("verifier_version", "v2"),
        ("environment_version", "e2"),
        ("agent_config_id", "other"),
    ):
        other = BaselineStore(tmp_path, replace(IDENT, **{fld: val}))
        assert other.key(t, 0, "dsA") != k and other.get(other.key(t, 0, "dsA")) is None, fld
    edited = t.model_copy(update={"task": t.task.model_copy(update={"max_tokens": 1})})
    assert task_fingerprint(edited) != task_fingerprint(t)
    assert store.key(edited, 0, "dsA") != k


def test_corrupt_or_foreign_entries_are_misses_and_get_overwritten(tmp_path: Path) -> None:
    t = a_task()
    store = BaselineStore(tmp_path, IDENT)
    k = store.key(t, 0, "d")
    store.put(k, out(True, 5))
    path = tmp_path / k[:2] / f"{k}.json"
    path.write_text("{not json")
    assert store.get(k) is None and store.corrupt == 1
    path.write_text(json.dumps({"key": "x" * 64, "outcome": {"status": "S"}}))
    assert store.get(k) is None and store.corrupt == 2
    store.put(k, out(False, 9))  # recompute overwrites the bad entry atomically
    got = store.get(k)
    assert got is not None and got.tokens == 9
    assert not list(tmp_path.rglob("*.tmp"))


def test_shared_arm_persists_across_sessions_and_flags_reuse(tmp_path: Path) -> None:
    t = a_task()
    calls = {"n": 0}

    def inner(task):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return out(True, 50)

    def session() -> tuple[SharedArm, BaselineStore]:
        store = BaselineStore(tmp_path, IDENT)
        return SharedArm(inner, store), store

    s1, st1 = session()
    CURRENT_SEED.set(0)
    first = s1(t)
    assert not first.cache_hit and calls["n"] == 1 and st1.puts == 1
    again = s1(t)  # same session: served from memory
    assert again.cache_hit and calls["n"] == 1 and again.tokens == first.tokens
    s2, st2 = session()  # new session: served from disk, never re-run
    third = s2(t)
    assert third.cache_hit and calls["n"] == 1 and st2.hits == 1 and st2.tokens_avoided == 50
    CURRENT_SEED.set(1)
    assert not s2(t).cache_hit and calls["n"] == 2  # a different repetition is a new draw


def test_concurrent_requests_compute_a_baseline_once(tmp_path: Path) -> None:
    t = a_task()
    calls = {"n": 0}
    gate = threading.Barrier(6)

    def inner(task):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return out(True, 7)

    shared = SharedArm(inner, BaselineStore(tmp_path, IDENT))
    results: list[ArmOutcome] = []

    def worker() -> None:
        CURRENT_SEED.set(0)
        gate.wait()
        results.append(shared(t))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert calls["n"] == 1 and len(results) == 6
    assert sum(1 for r in results if not r.cache_hit) == 1


# ---- retrospective simulation ---------------------------------------------------------------
def test_retrospective_never_promotes_and_keeps_the_known_bad_asset_rejected() -> None:
    aggs = {a.asset: a for a in aggregates_from_dir(ROOT / "docs/evaluations/gated-validation")}
    bad = aggs["mem_episodic_pool@1"]
    for scenario in ("pairs", "tasks"):
        assert replay_v2(bad, scenario)[0] == Verdict.FAIL
    # the candidate converts count-rule rejections into PASS or "more evidence", never into FAIL
    for name in (
        "mem_consolidated_pool@1",
        "mem_consolidated_timeout@1",
        "mem_procedural_timeout@1",
    ):
        assert replay_v1(aggs[name])[0] == Verdict.FAIL
        assert replay_v2(aggs[name], "pairs")[0] != Verdict.FAIL
    # conservative clustering can only make the verdict less favourable, never more
    order = {Verdict.FAIL: 0, Verdict.INCONCLUSIVE: 1, Verdict.PASS: 2}
    for a in aggs.values():
        assert order[replay_v2(a, "tasks")[0]] <= order[replay_v2(a, "pairs")[0]]
