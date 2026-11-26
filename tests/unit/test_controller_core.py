"""M6 unit tests: feasibility, estimator, calibration, policy, bandit and replay."""

import random

import pytest
from pydantic import ValidationError

from controller_support import (
    CAP,
    CAP_KEY,
    PLANNING_KEY,
    ctx,
    definition,
    match_with,
    record,
    snapshot_of,
    synthetic_log,
    task,
)
from experience_engine.controller.calibration import (
    CalibrationGate,
    expected_calibration_error,
    prequential_calibration,
    reliability_table,
)
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.features import explore_draw
from experience_engine.controller.policy import AdaptiveController
from experience_engine.controller.replay import off_policy_estimate, replay_decision
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    ControllerKind,
    ControllerParams,
    DecisionFeatures,
    action_key,
    parse_action_key,
)
from experience_engine.domain.decision import Strategy
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.stats import newcombe_difference_bounds, wilson_one_sided
from experience_engine.execution.controller import SELECTED_CAPABILITY_KEY, RuleCapabilityPolicy

BANDIT = ControllerKind.BANDIT


# ---- statistics ---------------------------------------------------------------------------------
def test_wilson_and_newcombe_bounds_match_published_reference_values() -> None:
    # Newcombe (1998), method 10: 56/70 vs 48/80 -> difference 0.2000, 95% CI (0.0524, 0.3339)
    lo, hi = newcombe_difference_bounds(56, 70, 48, 80, 0.975)
    assert lo == pytest.approx(0.0524, abs=5e-4) and hi == pytest.approx(0.3339, abs=5e-4)
    wl, wu = wilson_one_sided(48, 80, 0.975)
    assert wl == pytest.approx(0.4905, abs=5e-4) and wu == pytest.approx(0.7004, abs=5e-4)
    assert wilson_one_sided(0, 0, 0.95) == (0.0, 1.0)
    assert newcombe_difference_bounds(5, 0, 1, 10, 0.95) == (-1.0, 1.0)


def test_action_keys_round_trip() -> None:
    assert action_key(Strategy.GENERAL_PLANNING, None) == "GENERAL_PLANNING"
    assert parse_action_key(CAP_KEY) == (Strategy.DIRECT_CAPABILITY, CAP)
    assert parse_action_key("GENERAL_PLANNING") == (Strategy.GENERAL_PLANNING, None)


def test_params_cannot_make_high_risk_learned_or_explore_unlearned_classes() -> None:
    with pytest.raises(ValidationError):
        ControllerParams.model_validate({"learned_risk_levels": ["high"]})
    with pytest.raises(ValidationError):
        ControllerParams.model_validate({"exploration_risk_levels": ["medium"]})
    with pytest.raises(ValidationError):
        ControllerParams.model_validate(
            {"learned_risk_levels": ["low", "medium"], "delta": {"low": 0.1}}
        )
    with pytest.raises(ValidationError):
        ControllerParams.model_validate({"epsilon": 0.9})
    p = ControllerParams.model_validate({"learned_risk_levels": ["medium", "low", "low"]})
    assert p.learned_risk_levels == ["low", "medium"]


# ---- estimator ----------------------------------------------------------------------------------
def test_snapshot_counts_only_verified_acted_records_of_one_tenant() -> None:
    recs = [
        record(0, CAP_KEY, True, 100),
        record(1, CAP_KEY, False, 300),
        record(2, CAP_KEY, False, 999, status=OutcomeStatus.UNKNOWN),
        record(3, PLANNING_KEY, True, 500, acted=False),
        record(4, CAP_KEY, False, 200, status=OutcomeStatus.PARTIAL),
    ]
    snap = build_snapshot(recs, "bench")
    cell = snap.cells[f"pool|low|{CAP_KEY}"]
    assert (cell.n, cell.successes, cell.token_sum) == (3, 1, 600)
    assert snap.n_verified == 3 and snap.provenance is not None
    assert (snap.provenance.n_unknown_excluded, snap.provenance.n_unacted_excluded) == (1, 1)
    assert snap.snapshot_hash() == build_snapshot(recs, "bench").snapshot_hash()
    with pytest.raises(ValueError, match="single tenant"):
        build_snapshot([record(0, CAP_KEY, True, 1, tenant="other")], "bench")


# ---- calibration --------------------------------------------------------------------------------
def test_prequential_calibration_scores_predictions_made_before_the_outcome() -> None:
    # 5 warm-up successes then one failure: the prediction for the failure must be 6/7, not
    # an estimate that already includes the failure.
    recs = [record(i, CAP_KEY, True, 100) for i in range(5)] + [record(5, CAP_KEY, False, 100)]
    s = prequential_calibration(recs, gate=CalibrationGate(min_n=1))
    assert s.n == 1 and s.brier == pytest.approx((6 / 7) ** 2, abs=1e-5)


def test_calibration_gate_accepts_calibrated_and_rejects_biased_estimates() -> None:
    good = synthetic_log({CAP_KEY: 0.7}, {CAP_KEY: 500}, 600, seed=1)
    ok = prequential_calibration(good)
    assert ok.ok and ok.ece is not None and ok.ece < 0.10
    # a drifting world: early 95% success, then 10%: stale estimates are badly miscalibrated
    rng = random.Random(3)
    drift = [
        record(i, CAP_KEY, rng.random() < (0.95 if i < 300 else 0.10), 500) for i in range(600)
    ]
    bad = prequential_calibration(drift)
    assert not bad.ok and any("ECE" in r for r in bad.reasons)
    assert not prequential_calibration(good[:30]).ok  # too little history


def test_ece_and_reliability_table() -> None:
    preds, obs = [0.9] * 10 + [0.1] * 10, [True] * 9 + [False] + [False] * 9 + [True]
    assert expected_calibration_error(preds, obs) == pytest.approx(0.0)
    rows = reliability_table(preds, obs)
    assert [r.n for r in rows] == [10, 10]
    assert expected_calibration_error([0.9] * 10, [False] * 10) == pytest.approx(0.9)


def test_cost_calibration_reports_bias_and_mape() -> None:
    recs = [record(i, CAP_KEY, i % 2 == 0, 1000) for i in range(40)]
    s = prequential_calibration(recs, gate=CalibrationGate(min_n=10))
    assert s.cost_mape is not None and s.cost_mape < 0.15
    assert s.cost_bias is not None and abs(s.cost_bias) < 0.15


# ---- feasibility and the rule baseline ----------------------------------------------------------
def test_empty_evidence_reproduces_the_rule_baseline_exactly() -> None:
    adaptive = AdaptiveController(definition(snapshot_of({})))
    rule = RuleCapabilityPolicy()
    for ids in ((), (CAP,), ("pool.00000000aa@1", "pool.00000000bb@1")):
        match = match_with(*ids)
        c = ctx(task(), match)
        a, r = adaptive.decide(c), rule.decide(c)
        assert a.selected_action == r.selected_action
        assert a.feature_snapshot.get(SELECTED_CAPABILITY_KEY) == r.feature_snapshot.get(
            SELECTED_CAPABILITY_KEY
        )
        assert a.selection_probability == 1.0 and a.policy_version == "adaptive/1"
        assert Strategy.GENERAL_PLANNING in a.eligible_actions


def test_only_matcher_eligible_capabilities_are_ever_selectable() -> None:
    # evidence exists for a capability the matcher did NOT admit; it must never appear
    snap = snapshot_of(
        {
            PLANNING_KEY: (400, 300, 100_000),
            "DIRECT_CAPABILITY:ghost.000000000@1": (400, 399, 1_000),
        }
    )
    d = AdaptiveController(definition(snap)).decide(ctx(task(), match_with()))
    assert d.selected_action == Strategy.GENERAL_PLANNING
    f = DecisionFeatures.model_validate(d.feature_snapshot[CONTROLLER_KEY])
    assert [a.key for a in f.actions] == [PLANNING_KEY]


# ---- protected risk classes ---------------------------------------------------------------------
def _random_snapshot(rng: random.Random):  # type: ignore[no-untyped-def]
    cells = {
        PLANNING_KEY: (rng.randint(0, 500), 0, rng.randint(0, 500_000)),
        CAP_KEY: (rng.randint(0, 500), 0, rng.randint(0, 500_000)),
    }
    cells = {k: (n, rng.randint(0, n), t) for k, (n, _, t) in cells.items()}
    return snapshot_of(cells, risk="low"), cells


@pytest.mark.parametrize("risk", ["high", "medium"])
def test_protected_classes_always_get_the_rule_decision_and_never_read_estimates(risk: str) -> None:
    rng = random.Random(7)
    rule = RuleCapabilityPolicy()
    for i in range(200):
        snap, cells = _random_snapshot(rng)
        snap = snapshot_of(cells, risk=risk)
        kind = rng.choice([ControllerKind.COST_AWARE, BANDIT])
        defn = definition(
            snap,
            kind,
            exploration_risk_levels=["low"],
            exploration_families=["pool"],
            epsilon=0.5,
            n_min=1,
            min_total_verified=0,
        )
        c = ctx(task(risk), match_with(CAP), decision_id=f"d-{i}")
        a = AdaptiveController(defn).decide(c)
        r = rule.decide(c)
        assert (a.selected_action, a.feature_snapshot.get(SELECTED_CAPABILITY_KEY)) == (
            r.selected_action,
            r.feature_snapshot.get(SELECTED_CAPABILITY_KEY),
        )
        f = DecisionFeatures.model_validate(a.feature_snapshot[CONTROLLER_KEY])
        assert a.selection_probability == 1.0 and f.protected and f.evidence == [] and f.u is None
        assert a.estimates == {}


def test_a_foreign_tenant_task_gets_the_rule_decision() -> None:
    snap = snapshot_of({PLANNING_KEY: (400, 380, 1000), CAP_KEY: (400, 100, 9000)})
    d = AdaptiveController(definition(snap)).decide(ctx(task(tenant="other"), match_with(CAP)))
    assert d.selected_action == Strategy.DIRECT_CAPABILITY and "tenant" in d.reason


# ---- cost-aware policy --------------------------------------------------------------------------
def _decide(cells: dict[str, tuple[int, int, int]], **params: object) -> tuple[str, str]:
    d = AdaptiveController(definition(snapshot_of(cells), **params)).decide(
        ctx(task(), match_with(CAP))
    )
    ref = d.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
    return action_key(d.selected_action, ref if isinstance(ref, str) else None), d.reason


def test_default_is_kept_without_enough_evidence_on_the_default() -> None:
    key, why = _decide({PLANNING_KEY: (200, 180, 1000), CAP_KEY: (10, 1, 9000)})
    assert key == CAP_KEY and "insufficient evidence for the default" in why


def test_default_is_kept_when_the_alternative_has_too_little_evidence() -> None:
    key, _ = _decide({PLANNING_KEY: (5, 5, 100), CAP_KEY: (300, 270, 5000)})
    assert key == CAP_KEY


def test_cheaper_non_inferior_alternative_replaces_the_default() -> None:
    cells = {PLANNING_KEY: (300, 270, 300 * 1000), CAP_KEY: (300, 255, 300 * 3000)}
    key, why = _decide(cells)
    assert key == PLANNING_KEY and "non-inferior" in why


def test_cheaper_but_less_successful_alternative_is_never_chosen() -> None:
    # planning is 3x cheaper but loses 40 points of success: tokens must not buy correctness
    cells = {PLANNING_KEY: (300, 120, 300 * 1000), CAP_KEY: (300, 240, 300 * 3000)}
    assert _decide(cells)[0] == CAP_KEY


def test_savings_below_the_threshold_do_not_move_the_choice() -> None:
    cells = {PLANNING_KEY: (300, 270, 300 * 2850), CAP_KEY: (300, 270, 300 * 3000)}  # 5% cheaper
    assert _decide(cells)[0] == CAP_KEY
    assert _decide(cells, min_cost_saving=0.02)[0] == PLANNING_KEY


def test_noninferiority_needs_real_evidence_not_a_point_estimate() -> None:
    # same point estimates, 10x fewer trials: the lower bound no longer clears the margin
    big = {PLANNING_KEY: (300, 270, 300 * 1000), CAP_KEY: (300, 270, 300 * 3000)}
    small = {PLANNING_KEY: (30, 27, 30 * 1000), CAP_KEY: (30, 27, 30 * 3000)}
    assert _decide(big)[0] == PLANNING_KEY
    assert _decide(small, n_min=30)[0] == CAP_KEY


def test_required_success_floor_can_displace_a_default_that_misses_it() -> None:
    cells = {PLANNING_KEY: (300, 270, 300 * 4000), CAP_KEY: (300, 60, 300 * 400)}
    # without a floor the default stays (cheaper per verified success: 2000 vs 4444 tokens);
    # with a 0.8 floor only planning demonstrably qualifies
    assert _decide(cells)[0] == CAP_KEY
    key, why = _decide(cells, required_success={"low": 0.8})
    assert key == PLANNING_KEY and "not demonstrably" in why
    # nothing reaches the floor -> defer to the rule baseline
    key2, why2 = _decide(cells, required_success={"low": 0.99})
    assert key2 == CAP_KEY and "no action demonstrably" in why2


def test_decision_records_everything_rule_10_requires() -> None:
    cells = {PLANNING_KEY: (300, 270, 300 * 1000), CAP_KEY: (300, 255, 300 * 3000)}
    d = AdaptiveController(definition(snapshot_of(cells))).decide(ctx(task(), match_with(CAP)))
    assert set(d.eligible_actions) == {Strategy.GENERAL_PLANNING, Strategy.DIRECT_CAPABILITY}
    assert d.estimates[f"p_hat:{PLANNING_KEY}"] == pytest.approx(271 / 302, abs=1e-5)
    assert d.estimates[f"n:{CAP_KEY}"] == 300.0 and f"cost_hat:{CAP_KEY}" in d.estimates
    assert d.estimates[f"cps_hat:{PLANNING_KEY}"] == pytest.approx(
        d.estimates[f"cost_hat:{PLANNING_KEY}"] / d.estimates[f"p_hat:{PLANNING_KEY}"], rel=1e-3
    )
    assert d.policy_version == "adaptive/1" and d.selection_probability == 1.0
    f = DecisionFeatures.model_validate(d.feature_snapshot[CONTROLLER_KEY])
    assert f.default_key == CAP_KEY and f.snapshot_hash and len(f.evidence) == 2


# ---- bandit -------------------------------------------------------------------------------------
BANDIT_CELLS = {PLANNING_KEY: (60, 40, 60 * 5000), CAP_KEY: (200, 170, 200 * 3000)}
OPEN = {
    "exploration_risk_levels": ["low"],
    "exploration_families": ["pool"],
    "epsilon": 0.2,
    "min_total_verified": 100,
}


def _bandit(**over: object) -> AdaptiveController:
    return AdaptiveController(definition(snapshot_of(BANDIT_CELLS), BANDIT, **{**OPEN, **over}))


def test_exploration_is_off_unless_family_and_risk_are_explicitly_allow_listed() -> None:
    for over in ({"exploration_families": []}, {"exploration_risk_levels": []}, {"epsilon": 0.0}):
        c = _bandit(**over)
        seen = {
            c.decide(ctx(task(), match_with(CAP), f"d-{i}")).selected_action for i in range(300)
        }
        assert seen == {Strategy.DIRECT_CAPABILITY}
    other = AdaptiveController(
        definition(snapshot_of(BANDIT_CELLS, family="queue"), BANDIT, **OPEN)
    )
    t = task(family="queue")
    assert {
        other.decide(ctx(t, match_with(CAP), f"d-{i}")).selected_action for i in range(300)
    } == {Strategy.DIRECT_CAPABILITY}


def test_exploration_requires_the_data_gate() -> None:
    for defn in (
        definition(snapshot_of(BANDIT_CELLS, calibrated=False), BANDIT, **OPEN),
        definition(snapshot_of(BANDIT_CELLS, n_verified=50), BANDIT, **OPEN),
        definition(snapshot_of({PLANNING_KEY: (60, 40, 1), CAP_KEY: (10, 8, 1)}), BANDIT, **OPEN),
    ):
        c = AdaptiveController(defn)
        for i in range(200):
            d = c.decide(ctx(task(), match_with(CAP), f"g-{i}"))
            assert (
                d.selection_probability == 1.0 and d.selected_action == Strategy.DIRECT_CAPABILITY
            )


def test_logged_propensities_are_exact_and_empirically_correct() -> None:
    c = _bandit()
    n = 6000
    counts: dict[str, int] = {}
    probs: dict[str, float] = {}
    for i in range(n):
        d = c.decide(ctx(task(), match_with(CAP), f"d-{i:06d}"))
        ref = d.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
        k = action_key(d.selected_action, ref if isinstance(ref, str) else None)
        counts[k] = counts.get(k, 0) + 1
        p = d.selection_probability
        assert p is not None
        probs[k] = p
        pis = {kk[3:]: v for kk, v in d.estimates.items() if kk.startswith("pi:")}
        assert sum(pis.values()) == pytest.approx(1.0, abs=1e-4) and pis[k] == p
    assert probs[PLANNING_KEY] == pytest.approx(0.1) and probs[CAP_KEY] == pytest.approx(0.9)
    assert counts[PLANNING_KEY] / n == pytest.approx(0.1, abs=0.015)


def test_exploration_never_picks_an_action_that_is_confidently_worse() -> None:
    cells = {PLANNING_KEY: (400, 40, 400 * 5000), CAP_KEY: (400, 360, 400 * 3000)}  # 10% vs 90%
    c = AdaptiveController(definition(snapshot_of(cells), BANDIT, **OPEN))
    chosen = {c.decide(ctx(task(), match_with(CAP), f"w-{i}")).selected_action for i in range(500)}
    assert chosen == {Strategy.DIRECT_CAPABILITY}


def test_draws_are_deterministic_functions_of_the_decision_id() -> None:
    assert explore_draw("abc", "adaptive/1") == explore_draw("abc", "adaptive/1")
    assert explore_draw("abc", "adaptive/1") != explore_draw("abd", "adaptive/1")
    u = [explore_draw(f"x{i}", "adaptive/1") for i in range(2000)]
    assert 0.45 < sum(u) / len(u) < 0.55 and all(0 <= v < 1 for v in u)


# ---- replay / reproducibility -------------------------------------------------------------------
def test_stored_decisions_are_reproducible_from_features_and_policy_version() -> None:
    defn = definition(snapshot_of(BANDIT_CELLS), BANDIT, **OPEN)
    c = AdaptiveController(defn)
    seen = set()
    for i in range(400):
        d = c.decide(ctx(task(), match_with(CAP), f"r-{i}"))
        seen.add(d.selected_action)
        check = replay_decision(d, defn)
        assert check.reproducible, check.differences
    assert seen == {Strategy.DIRECT_CAPABILITY, Strategy.GENERAL_PLANNING}  # both branches replayed


def test_replay_detects_tampering_and_version_drift() -> None:
    defn = definition(snapshot_of(BANDIT_CELLS), BANDIT, **OPEN)
    d = AdaptiveController(defn).decide(ctx(task(), match_with(CAP), "t-1"))
    assert replay_decision(d, defn).reproducible
    forged = d.model_copy(update={"selection_probability": 0.5})
    assert "probability" in " ".join(replay_decision(forged, defn).differences)
    other_snap = definition(
        snapshot_of({PLANNING_KEY: (60, 41, 60 * 5000), CAP_KEY: (200, 170, 600000)}),
        BANDIT,
        **OPEN,
    )
    assert not replay_decision(d, other_snap).reproducible
    assert not replay_decision(
        d, definition(snapshot_of(BANDIT_CELLS), BANDIT, version=2, **OPEN)
    ).reproducible
    legacy = RuleCapabilityPolicy().decide(ctx(task(), match_with(CAP)))
    assert not replay_decision(legacy, defn).reproducible


# ---- off-policy estimation ----------------------------------------------------------------------
def test_ips_is_unbiased_on_randomised_logs_and_blind_on_deterministic_ones() -> None:
    rates = {CAP_KEY: 0.6, PLANNING_KEY: 0.9}
    cost = {CAP_KEY: 3000, PLANNING_KEY: 1000}
    # candidate always plans (large non-inferior saving, evidence injected via the snapshot)
    cand_snap = snapshot_of({PLANNING_KEY: (400, 360, 400 * 1000), CAP_KEY: (400, 240, 400 * 3000)})
    cand = definition(cand_snap)
    logged = synthetic_log(rates, cost, 4000, seed=11, mix={CAP_KEY: 0.5, PLANNING_KEY: 0.5})
    model = build_snapshot(logged, "bench")
    est = off_policy_estimate(logged, cand, model)
    assert est.coverage == pytest.approx(1.0)
    assert est.ips_success == pytest.approx(0.9, abs=0.03) and est.snips_success == pytest.approx(
        0.9, abs=0.03
    )
    assert est.dr_success == pytest.approx(0.9, abs=0.03) and est.dr_tokens == pytest.approx(
        1000, abs=60
    )
    assert est.logged_success == pytest.approx(0.75, abs=0.03)
    # the rule-only log never tried planning: the candidate has no support and IPS says so
    rule_log = synthetic_log(rates, cost, 1000, seed=12)
    blind = off_policy_estimate(rule_log, cand, build_snapshot(rule_log, "bench"))
    assert blind.coverage == 0.0 and blind.ips_success == 0.0
    assert blind.protected_deviations == 0 and blind.infeasible_choices == 0


def test_off_policy_estimate_reports_no_protected_deviation_for_high_risk_logs() -> None:
    cand = definition(
        snapshot_of({PLANNING_KEY: (400, 360, 400 * 1000), CAP_KEY: (400, 240, 1_200_000)})
    )
    logged = [record(i, CAP_KEY, True, 100, risk="high") for i in range(20)]
    est = off_policy_estimate(logged, cand, build_snapshot(logged, "bench"))
    assert est.protected_deviations == 0 and est.agreement == 1.0
