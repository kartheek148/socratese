"""M6 unit tests: prequential full-information simulation, the frozen verdict rule, datasets.

Synthetic worlds validate the *mechanism* (ground truth known). They are not evidence about real
models; real recorded data is replayed in the evaluation report.
"""

import json
from pathlib import Path

import pytest

from controller_support import CAP_KEY, PLANNING_KEY, world
from experience_engine.controller.datasets import describe, from_experiment, with_protected_share
from experience_engine.controller.simulate import (
    SimConfig,
    Verdict,
    compare,
    decide_verdict,
    evaluate,
    make_orders,
    rule_log,
    run_sequence,
)
from experience_engine.domain.controller import ControllerKind, ControllerParams
from experience_engine.domain.experiment import ExperimentResult

BANDIT = ControllerKind.BANDIT
COST_AWARE = ControllerKind.COST_AWARE
EXPLORE = {"exploration_risk_levels": ["low"], "exploration_families": ["pool"], "epsilon": 0.15}
DATA = Path(__file__).resolve().parents[2] / "docs" / "evaluations" / "data"


def cfg(kind: ControllerKind, **params: object) -> SimConfig:
    return SimConfig(kind, ControllerParams.model_validate({**EXPLORE, **params}), refresh_every=10)


def test_orders_are_seeded_permutations() -> None:
    a, b = make_orders(50, 3, seed=1), make_orders(50, 3, seed=1)
    assert a == b and all(sorted(o) == list(range(50)) for o in a) and a[0] != a[1]


def test_verdict_rule_is_the_amended_frozen_one() -> None:
    ok, cheap, same = (0.0, -0.02, 0.02), (0.9, 0.8, 0.97), (1.0, 0.99, 1.0)
    assert decide_verdict(ok, cheap) == Verdict.BEATS
    assert decide_verdict(ok, same) == Verdict.EQUAL
    assert (
        decide_verdict((-0.2, -0.3, -0.1), (0.5, 0.4, 0.6)) == Verdict.WORSE
    )  # cost never rescues
    assert decide_verdict((0.0, -0.02, 0.02), (1.3, 1.2, 1.4)) == Verdict.WORSE
    # a tiny significant loss inside the margin is EQUAL, not WORSE
    assert decide_verdict((-0.007, -0.012, -0.002), same) == Verdict.EQUAL
    # wide interval: non-inferiority not shown but not confidently worse either
    assert decide_verdict((-0.04, -0.2, 0.1), cheap) == Verdict.INCONCLUSIVE
    assert decide_verdict((None, None, None), (None, None, None)) == Verdict.INCONCLUSIVE


def test_when_the_rule_is_right_the_controller_equals_it_once_exploration_amortises() -> None:
    # capability is better AND cheaper: nothing to improve. The passive policy never deviates; the
    # bandit pays a bounded, measured exploration cost that is small at scale.
    big = world(
        3000, {CAP_KEY: 0.9, PLANNING_KEY: 0.3}, {CAP_KEY: 2000, PLANNING_KEY: 5000}, seed=1
    )
    for kind in (COST_AWARE, BANDIT):
        cmp, _ = evaluate(big, cfg(kind), n_orders=3, seed=2)
        assert cmp.verdict == Verdict.EQUAL, (kind, cmp.success_diff, cmp.cost_ratio)
        assert cmp.deviation_rate < 0.05
    passive, _ = evaluate(big, cfg(COST_AWARE), n_orders=2)
    assert passive.deviation_rate == 0.0  # no planning evidence, so it can never deviate


def test_exploration_is_not_free_at_small_scale() -> None:
    # same world, 600 tasks: learning that planning is worse costs successes the baseline keeps.
    small = world(
        600, {CAP_KEY: 0.9, PLANNING_KEY: 0.3}, {CAP_KEY: 2000, PLANNING_KEY: 5000}, seed=1
    )
    bandit, _ = evaluate(small, cfg(BANDIT), n_orders=6, seed=2)
    assert bandit.exploration_successes_lost_per_order > 10
    assert bandit.success_diff[0] is not None and -0.05 < bandit.success_diff[0] < 0.0
    assert bandit.verdict == Verdict.WORSE  # confidently costlier per verified success (design A2)
    assert evaluate(small, cfg(COST_AWARE), n_orders=2)[0].verdict == Verdict.EQUAL


def test_when_the_rule_is_wrong_the_bandit_recovers_and_the_passive_policy_cannot() -> None:
    # a degraded capability: much costlier and no better than planning (synthetic)
    tasks = world(
        3000, {CAP_KEY: 0.55, PLANNING_KEY: 0.55}, {CAP_KEY: 6000, PLANNING_KEY: 2500}, seed=3
    )
    bandit, runs = evaluate(tasks, cfg(BANDIT, min_total_verified=60), n_orders=4, seed=4)
    assert bandit.verdict == Verdict.BEATS, (bandit.success_diff, bandit.cost_ratio)
    assert bandit.cost_ratio[2] is not None and bandit.cost_ratio[2] < 1.0
    late = [s for steps in runs for s in steps[1500:]]
    assert sum(s.chosen == PLANNING_KEY for s in late) / len(late) > 0.9
    passive, _ = evaluate(tasks, cfg(COST_AWARE), n_orders=2, seed=4)
    assert passive.verdict == Verdict.EQUAL and passive.deviation_rate == 0.0


def test_it_does_not_trade_correctness_for_tokens() -> None:
    # planning is 3x cheaper but 30 points worse: the controller must keep the capability
    tasks = world(
        2500, {CAP_KEY: 0.85, PLANNING_KEY: 0.55}, {CAP_KEY: 6000, PLANNING_KEY: 2000}, seed=5
    )
    cmp, runs = evaluate(tasks, cfg(BANDIT, min_total_verified=60), n_orders=3, seed=6)
    assert cmp.verdict in (Verdict.EQUAL, Verdict.BEATS)
    assert cmp.success_diff[0] is not None and cmp.success_diff[0] > -0.02
    late = [s for steps in runs for s in steps[1500:]]
    assert sum(s.chosen == CAP_KEY for s in late) / len(late) > 0.8


def test_exploration_cost_is_measured_and_bounded_by_epsilon() -> None:
    tasks = world(
        2000, {CAP_KEY: 0.9, PLANNING_KEY: 0.3}, {CAP_KEY: 2000, PLANNING_KEY: 5000}, seed=7
    )
    cmp, _ = evaluate(tasks, cfg(BANDIT, min_total_verified=60), n_orders=3, seed=8)
    assert 0 < cmp.exploration_rate <= 0.15
    assert (
        cmp.exploration_successes_lost_per_order > 0 and cmp.exploration_tokens_delta_per_order > 0
    )


def test_protected_tasks_never_deviate_even_when_exploration_is_open() -> None:
    base = world(
        2000, {CAP_KEY: 0.55, PLANNING_KEY: 0.55}, {CAP_KEY: 6000, PLANNING_KEY: 2500}, seed=9
    )
    mixed = with_protected_share(base, 0.5, "high")
    assert 0.4 < sum(t.risk == "high" for t in mixed) / len(mixed) < 0.6
    cmp, runs = evaluate(mixed, cfg(BANDIT, min_total_verified=60), n_orders=3, seed=10)
    assert cmp.protected_deviations == 0
    assert all(
        s.chosen == s.default and s.probability == 1.0
        for st in runs
        for s in st
        if s.risk == "high"
    )
    assert cmp.deviation_rate > 0  # the low-risk half still learned


def test_a_training_window_adapts_to_drift_where_cumulative_estimates_lag() -> None:
    # the capability collapses halfway; planning stays constant (synthetic)
    n, half = 4000, 2000
    tasks = world(
        n,
        {CAP_KEY: 0.9, PLANNING_KEY: 0.6},
        {CAP_KEY: 2000, PLANNING_KEY: 4000},
        seed=11,
        drift_at=half,
        drifted_rates={CAP_KEY: 0.2, PLANNING_KEY: 0.6},
    )
    order = list(range(n))  # time order matters here
    params = ControllerParams.model_validate({**EXPLORE, "min_total_verified": 60, "epsilon": 0.2})
    cumulative = run_sequence(tasks, order, SimConfig(BANDIT, params), label="c")
    windowed = run_sequence(tasks, order, SimConfig(BANDIT, params, train_window=600), label="w")

    def planning_share(steps: list, lo: int, hi: int) -> float:  # type: ignore[type-arg]
        seg = steps[lo:hi]
        return sum(s.chosen == PLANNING_KEY for s in seg) / len(seg)

    assert planning_share(windowed, 3000, 4000) > planning_share(cumulative, 3000, 4000) + 0.2
    assert planning_share(windowed, 3000, 4000) > 0.8


def test_the_rule_baseline_arm_is_the_default_action() -> None:
    tasks = world(
        100, {CAP_KEY: 0.9, PLANNING_KEY: 0.3}, {CAP_KEY: 2000, PLANNING_KEY: 5000}, seed=12
    )
    steps = run_sequence(tasks, list(range(100)), cfg(COST_AWARE), label="x")
    cmp = compare([steps])
    assert cmp.baseline.success_rate == pytest.approx(
        sum(t.outcomes[CAP_KEY][0].value == "VERIFIED_SUCCESS" for t in tasks) / 100
    )
    assert rule_log(tasks)[0].action == CAP_KEY and rule_log(tasks)[0].selection_probability == 1.0


# ---- datasets from recorded experiments ---------------------------------------------------------
def test_recorded_experiment_becomes_a_full_information_table() -> None:
    raw = json.loads((DATA / "m59-confirmation-p4-set-a.json").read_text())
    res = ExperimentResult.model_validate(raw)
    tasks, rep = from_experiment(res, families=["pool", "timeout"])
    assert rep.n_pairs == len(tasks) == 120 and rep.n_without_capability == 0
    truth = describe(tasks)
    assert set(truth) == {"pool", "timeout"}
    pool = truth["pool"]
    assert pool["GENERAL_PLANNING"][0] == 60 and 0.2 < pool["GENERAL_PLANNING"][1] < 0.35
    cap_key = next(k for k in pool if k.startswith("DIRECT_CAPABILITY:pool."))
    assert pool[cap_key][1] > 0.7 and tasks[0].default_key.startswith("DIRECT_CAPABILITY:")
    # queue has no capability: the only action is planning and the default is planning
    q, qrep = from_experiment(res, families=["queue"])
    assert qrep.n_without_capability == len(q) == 30 and all(
        t.default_key == PLANNING_KEY for t in q
    )
