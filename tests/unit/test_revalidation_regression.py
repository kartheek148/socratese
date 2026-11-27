"""M6.3 regression guards: the new configuration is invisible until it is needed, SHADOW never
alters a decision, parameters are validated, and runs are reproducible."""

import pytest
from pydantic import ValidationError

from experience_engine.controller.drift_eval import controller_config
from experience_engine.controller.reval_eval import reval_config
from experience_engine.controller.scenarios import REVAL_SCENARIOS, SCENARIOS, build_stream
from experience_engine.controller.simulate import run_stream
from experience_engine.domain.controller import ControllerParams
from experience_engine.domain.drift import DriftMode, DriftParams
from experience_engine.domain.revalidation import RevalidationParams, RevalStrategy

DRIFT = DriftParams()
REVAL = RevalidationParams(strategy=RevalStrategy.SEQUENTIAL)


def trace(steps: list) -> list[tuple]:  # type: ignore[type-arg]
    return [(s.chosen, s.status, s.tokens, s.mode, round(s.probability, 9)) for s in steps]


def test_an_unset_revalidation_block_is_omitted_so_every_earlier_hash_is_unchanged() -> None:
    plain = ControllerParams.model_validate({"evidence": {}})
    assert "revalidation" not in plain.model_dump(mode="json")
    with_drift = ControllerParams.model_validate({"evidence": {}, "drift": {}})
    assert "revalidation" not in with_drift.model_dump(mode="json")
    set_ = ControllerParams.model_validate(
        {"evidence": {}, "drift": {}, "revalidation": {"strategy": "FIXED"}}
    )
    assert set_.model_dump(mode="json")["revalidation"]["strategy"] == "FIXED"
    assert set_.model_dump_json() != with_drift.model_dump_json()  # and the policy hash differs


def test_revalidation_needs_the_drift_monitor_and_consistent_parameters() -> None:
    with pytest.raises(ValidationError, match="drift monitor"):
        ControllerParams.model_validate({"evidence": {}, "revalidation": {"strategy": "FIXED"}})
    with pytest.raises(ValidationError):
        RevalidationParams(strategy=RevalStrategy.FIXED, alpha=0.01, alpha_severe=0.05)
    with pytest.raises(ValidationError):
        RevalidationParams(strategy=RevalStrategy.FIXED, confirm_trials=999, max_trials=100)
    with pytest.raises(ValidationError):
        RevalidationParams(strategy=RevalStrategy.FIXED, allowed_risks=())
    with pytest.raises(ValidationError):
        RevalidationParams.model_validate({"margin": 0.1})  # the strategy is not defaulted
    with pytest.raises(ValidationError):
        RevalidationParams.model_validate({"strategy": "FIXED", "surprise": 1})
    assert len({RevalidationParams(strategy=s).config_hash() for s in RevalStrategy}) == 3
    assert a != b


def test_the_frozen_defaults_are_the_design_values() -> None:
    p = RevalidationParams(strategy=RevalStrategy.FIXED)
    assert (p.margin, p.alpha, p.alpha_severe, p.beta, p.severe_drop) == (
        0.10,
        0.05,
        0.025,
        0.20,
        0.25,
    )
    assert (p.dwell, p.trials_per_round, p.round_hours, p.max_trials) == (50, 50, 10.0, 320)
    assert (p.max_cycles, p.max_cycle_hours, p.case_ttl_hours) == (3, 400.0, 500.0)
    assert (p.cell_token_budget, p.min_pool, p.cost_guard) == (3_000_000, 50, 1.15)
    assert (p.confirm_gap_hours, p.confirm_trials, p.confirm_alpha) == (20.0, 20, 0.01)
    assert (p.cost_futility_min_trials, p.cost_futility_confidence) == (15, 0.95)
    assert (p.ready_default_min, p.ready_confidence, p.max_unverified_fraction) == (30, 0.90, 0.20)
    assert p.allowed_risks == ("low",)


def test_revalidation_changes_nothing_until_a_cell_is_quarantined() -> None:
    sc = SCENARIOS["S1"]
    for seed in (7_200_000, 7_200_001):
        stream = build_stream(sc, seed)
        order = list(range(sc.n))
        base = run_stream(stream.tasks, order, reval_config("m62", stream, DRIFT, REVAL), label="x")
        reval = run_stream(
            stream.tasks, order, reval_config("m63_C", stream, DRIFT, REVAL), label="x"
        )
        assert not base.transitions or all(
            t.to_state.value != "QUARANTINED" for t in base.transitions
        )
        assert trace(base.steps) == trace(reval.steps)
        assert reval.reval_trials == [] and reval.reval_tokens == 0
        assert [t.signature() for t in base.transitions] == [
            t.signature() for t in reval.transitions
        ]


def test_in_shadow_mode_revalidation_never_alters_a_decision_even_under_drift() -> None:
    sc = SCENARIOS["S2"]  # an abrupt collapse: the monitor quarantines, SHADOW only records it
    stream = build_stream(sc, 7_200_002)
    order = list(range(sc.n))
    m61 = run_stream(stream.tasks, order, controller_config("m61", stream, DRIFT), label="y")
    cfg = reval_config("m63_C", stream, DRIFT.model_copy(update={"mode": DriftMode.SHADOW}), REVAL)
    # reval_config forces ENFORCE for revalidating variants; build the SHADOW policy explicitly
    raw = cfg.params.model_dump(mode="json")
    raw["drift"]["mode"] = "SHADOW"
    shadow_cfg = cfg.__class__(**{**cfg.__dict__, "params": ControllerParams.model_validate(raw)})
    shadow = run_stream(stream.tasks, order, shadow_cfg, label="y")
    assert trace(m61.steps) == trace(shadow.steps)
    assert shadow.reval_trials == [] and shadow.reval_tokens == 0  # nothing was executed
    assert shadow.reval_transitions and all(not t.applied for t in shadow.reval_transitions)


def test_a_run_is_reproducible_bit_for_bit() -> None:
    sc = REVAL_SCENARIOS["S5"]
    stream = build_stream(sc, 7_200_003)
    cfg_a = reval_config("m63_A", stream, DRIFT, REVAL)
    cfg_b = reval_config("m63_A", build_stream(sc, 7_200_003), DRIFT, REVAL)
    a = run_stream(stream.tasks, list(range(sc.n)), cfg_a, label="z")
    b = run_stream(stream.tasks, list(range(sc.n)), cfg_b, label="z")
    assert trace(a.steps) == trace(b.steps)
    assert [t.model_dump(mode="json") for t in a.reval_transitions] == [
        t.model_dump(mode="json") for t in b.reval_transitions
    ]
    assert [t.result for t in a.reval_trials] == [t.result for t in b.reval_trials]
    assert a.reval_tokens == b.reval_tokens
