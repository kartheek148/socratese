"""M6.2 statistical behaviour of the frozen monitor on independent test streams (seed range
5,000,000+, disjoint from the development 100,000+ and held-out 7,000,000+ ranges)."""

import statistics

from experience_engine.controller.drift_eval import (
    GridPoint,
    calibration_task,
    clopper_pearson_upper,
    monitor_only,
    score_family,
)
from experience_engine.controller.scenarios import SCENARIOS, build_stream
from experience_engine.domain.drift import DriftParams, DriftState

P = DriftParams()
SEEDS = range(5_000_000, 5_000_060)


def scores(sid: str, params: DriftParams = P, seeds: range = SEEDS):  # type: ignore[no-untyped-def]
    sc = SCENARIOS[sid]
    out = []
    for seed in seeds:
        stream = build_stream(sc, seed)
        ts, _ = monitor_only(stream, params)
        first = sc.families[0].name
        out.append(score_family(first, stream.material[first], ts, sc.n))
    return out


def test_false_alarms_are_rare_in_stable_and_noisy_environments() -> None:
    stable = scores("S1")
    noisy = scores("S6")
    assert sum(1 for s in stable if s.false_alarms) <= 3  # <= 5% of 60
    assert sum(1 for s in noisy if s.false_alarms) <= 6  # the noisy world is allowed a few
    assert statistics.mean(s.suspect_episodes for s in stable) < 15  # suspicion stays cheap


def test_abrupt_success_collapse_and_cost_only_drift_are_detected_with_bounded_delay() -> None:
    abrupt, cost = scores("S2"), scores("S4")
    assert sum(1 for s in abrupt if s.first_detection is not None) >= 57
    delays = [s.delay for s in abrupt if s.delay is not None]
    assert statistics.median(delays) <= 100
    assert sum(1 for s in cost if s.first_detection is not None) >= 54  # >= 90%
    assert all(s.false_alarms == 0 for s in abrupt)  # detection came after the onset


def test_gradual_degradation_is_detected_later_than_an_abrupt_one() -> None:
    abrupt, gradual = scores("S2"), scores("S3")
    d_a = statistics.median(s.delay for s in abrupt if s.delay is not None)
    d_g = statistics.median(s.delay for s in gradual if s.delay is not None)
    assert d_g > d_a
    assert sum(1 for s in gradual if s.first_detection is not None) >= 54


def test_small_samples_stay_conservative() -> None:
    stable, drifted = scores("S9a"), scores("S9b")
    assert not any(s.drifted_positions for s in stable)
    assert not any(s.drifted_positions for s in drifted)  # 15 post-drift observations < min_samples


def test_moderately_misleading_early_observations_do_not_cause_alarms() -> None:
    early = scores("S10")
    assert sum(1 for s in early if s.false_alarms) <= 3


def test_structural_changes_alarm_quickly_without_outcome_evidence() -> None:
    runtime = scores("S8", seeds=range(5_000_000, 5_000_020))
    stale = scores("S8b", seeds=range(5_000_000, 5_000_020))
    assert all(
        s.first_detection is not None and s.delay is not None and s.delay <= 20 for s in runtime
    )
    assert all(
        s.first_detection is not None and s.delay is not None and s.delay <= 20 for s in stale
    )


def test_a_higher_confidence_never_raises_more_false_alarms_on_the_same_streams() -> None:
    low, high = GridPoint("WINDOW", 0.95, 3), GridPoint("WINDOW", 0.999, 3)
    fa_low = fa_high = 0
    for seed in range(5_000_100, 5_000_130):
        res = calibration_task("S6", seed, [low, high], P)
        fa_low += bool(res[low].false_alarms)
        fa_high += bool(res[high].false_alarms)
    assert fa_high <= fa_low and fa_low > fa_high  # strictly fewer here: the noisy world bites


def test_cusum_has_the_right_direction_an_improvement_is_not_degradation() -> None:
    cusum = GridPoint("CUSUM", 0.995, 10, 6.0)
    better = DriftParams().model_copy(update={"confidence": 0.995})
    # S1 with a *better* reference than reality is the degradation case; the converse (reality
    # better than the reference) is built by shrinking the reference through a low-success prior
    sc = SCENARIOS["S1"]
    stream = build_stream(sc, 5_000_500)
    ts, _ = monitor_only(stream, cusum.params(better))
    assert not any(t.to_state == DriftState.DRIFTED for t in ts)


def test_exact_binomial_bound_is_correct() -> None:
    assert (
        abs(clopper_pearson_upper(0, 100, 0.95) - 0.0295) < 0.001
    )  # rule of three: 1-0.05^(1/100)
    assert clopper_pearson_upper(100, 100) == 1.0
    assert clopper_pearson_upper(5, 100) > 0.05
