"""M6.2 controller integration: overlay effects, probes, shadow vs enforce, replay, simulation."""

import pytest

from controller_support import (
    CAP,
    CAP_KEY,
    ctx,
    evidence_definition,
    match_with,
    pcell,
    task,
)
from experience_engine.controller.policy import AdaptiveController
from experience_engine.controller.replay import replay_decision
from experience_engine.controller.scenarios import CAP_KEY as SIM_CAP_KEY
from experience_engine.controller.scenarios import SCENARIOS, build_stream
from experience_engine.controller.simulate import SimConfig, run_stream
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    PLANNING_KEY,
    ControllerKind,
    ControllerParams,
    DecisionFeatures,
    paired_key,
)
from experience_engine.domain.decision import Strategy
from experience_engine.domain.drift import (
    DriftCellStatus,
    DriftOverlay,
    DriftParams,
    DriftState,
)
from experience_engine.execution.controller import SELECTED_CAPABILITY_KEY

CELL = pcell(90, 30, 30, 90, dt=9000, at=3000)  # planning as good as the capability, far cheaper
KEY = paired_key("pool", "low", CAP_KEY, PLANNING_KEY)


def defn(mode: str | None, **kw: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "exploration_risk_levels": ["low"],
        "exploration_families": ["pool"],
        "epsilon": 0.1,
        "min_total_verified": 0,
    }
    if mode:
        params["drift"] = {"mode": mode}
    params.update(kw)
    return evidence_definition(CELL, kind=ControllerKind.EVIDENCE_DRIVEN, **params)


def overlay(state: DriftState, probes: int = 0, seq: int = 3) -> DriftOverlay:
    return DriftOverlay(cells={KEY: DriftCellStatus(state=state, seq=seq, probes_remaining=probes)})


def decide(d, ov=None, *, risk="low", family="pool", i=0, tenant="bench"):  # type: ignore[no-untyped-def]
    c = AdaptiveController(d, ov)
    dec = c.decide(ctx(task(risk, family, tenant=tenant), match_with(CAP), f"e-{i}"))
    ref = dec.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
    key = (
        PLANNING_KEY
        if dec.selected_action == Strategy.GENERAL_PLANNING
        else f"DIRECT_CAPABILITY:{ref}"
    )
    return key, dec


def test_without_the_monitor_the_prior_controls_and_the_old_guard_stays() -> None:
    key, dec = decide(defn(None))
    assert key == PLANNING_KEY  # M6.1 behaviour: the paired prior switches
    f = DecisionFeatures.model_validate(dec.feature_snapshot[CONTROLLER_KEY])
    assert f.drift == [] and f.evidence_module


@pytest.mark.parametrize(
    "state", [DriftState.DRIFTED, DriftState.QUARANTINED, DriftState.RECOVERING]
)
def test_a_disabled_prior_cannot_control_and_the_rule_default_is_served(state: DriftState) -> None:
    key, dec = decide(defn("ENFORCE"), overlay(state))
    assert key == CAP_KEY
    assert "prior invalidated" in dec.reason and f"evidence {state.value}" in dec.reason
    f = DecisionFeatures.model_validate(dec.feature_snapshot[CONTROLLER_KEY])
    assert [(v.key, v.state) for v in f.drift] == [(PLANNING_KEY, state)]


def test_normal_and_suspect_leave_the_decision_unchanged() -> None:
    assert decide(defn("ENFORCE"))[0] == PLANNING_KEY
    assert decide(defn("ENFORCE"), overlay(DriftState.SUSPECT))[0] == PLANNING_KEY


def test_shadow_mode_never_alters_a_decision_even_with_a_quarantine_overlay() -> None:
    quarantined = overlay(DriftState.QUARANTINED)
    key, dec = decide(defn("SHADOW"), quarantined)
    assert key == PLANNING_KEY
    f = DecisionFeatures.model_validate(dec.feature_snapshot[CONTROLLER_KEY])
    assert f.drift == []  # shadow decisions do not even read the overlay


def test_quarantine_does_not_fall_back_to_stale_live_evidence() -> None:
    # live data that still show the alternative as fine (collected before the drift) must not
    # re-admit it through the unpaired rule while the paired prior is quarantined
    live = {CAP_KEY: (300, 150, 300 * 9000), PLANNING_KEY: (300, 160, 300 * 3000)}
    d = evidence_definition(
        CELL,
        kind=ControllerKind.EVIDENCE_DRIVEN,
        live=live,
        drift={"mode": "ENFORCE"},
        exploration_risk_levels=["low"],
        exploration_families=["pool"],
        epsilon=0.1,
        min_total_verified=0,
    )
    assert decide(d)[0] == PLANNING_KEY
    assert decide(d, overlay(DriftState.QUARANTINED))[0] == CAP_KEY


def test_recovering_probes_are_bounded_logged_and_allow_listed() -> None:
    d = defn("ENFORCE")
    ov = overlay(DriftState.RECOVERING, probes=10)
    seen: dict[str, float] = {}
    n_probe = 0
    for i in range(400):
        key, dec = decide(d, ov, i=i)
        n_probe += key == PLANNING_KEY
        seen[key] = dec.selection_probability or 0.0
    assert set(seen) == {CAP_KEY, PLANNING_KEY}
    rate = DriftParams().probe_rate
    assert seen[PLANNING_KEY] == pytest.approx(rate / 2) and seen[CAP_KEY] == pytest.approx(
        1 - rate / 2
    )
    assert 0.05 < n_probe / 400 < 0.20
    _, dec = decide(d, ov, i=1)
    assert any(k.startswith("pi:") for k in dec.estimates)
    # no budget left, a family or a risk level that is not allow-listed: no probe at all
    for bad in (
        (d, overlay(DriftState.RECOVERING, probes=0), "low", "pool"),
        (defn("ENFORCE", exploration_families=["timeout"]), ov, "low", "pool"),
        (defn("ENFORCE", exploration_risk_levels=[]), ov, "low", "pool"),
    ):
        dd, o, risk, fam = bad
        assert {decide(dd, o, risk=risk, family=fam, i=i)[0] for i in range(300)} == {CAP_KEY}


def test_protected_classes_and_foreign_tenants_never_read_the_overlay() -> None:
    d = defn("ENFORCE")
    ov = overlay(DriftState.RECOVERING, probes=50)
    for risk in ("high", "medium"):
        key, dec = decide(d, ov, risk=risk)
        f = DecisionFeatures.model_validate(dec.feature_snapshot[CONTROLLER_KEY])
        assert key == CAP_KEY and f.drift == [] and f.u is None
    key, dec = decide(d, ov, tenant="other")
    f = DecisionFeatures.model_validate(dec.feature_snapshot[CONTROLLER_KEY])
    assert key == CAP_KEY and f.tenant_mismatch and f.drift == []


def test_decisions_with_drift_views_and_probes_replay_exactly_and_tampering_is_caught() -> None:
    d = defn("ENFORCE")
    for ov in (overlay(DriftState.QUARANTINED), overlay(DriftState.RECOVERING, probes=20)):
        for i in range(60):
            _, dec = decide(d, ov, i=i)
            check = replay_decision(dec, d)
            assert check.reproducible, check.differences
    _, dec = decide(d, overlay(DriftState.QUARANTINED), i=2)
    f = DecisionFeatures.model_validate(dec.feature_snapshot[CONTROLLER_KEY])
    forged = f.model_copy(update={"drift": []})  # remove the quarantine from the stored features
    snap = dict(dec.feature_snapshot) | {CONTROLLER_KEY: forged.model_dump(mode="json")}
    check = replay_decision(dec.model_copy(update={"feature_snapshot": snap}), d)
    assert not check.reproducible


def test_the_drift_monitor_needs_the_evidence_module_and_m61_parameters_serialise_as_before() -> (
    None
):
    with pytest.raises(ValueError):
        ControllerParams.model_validate({"drift": {"mode": "SHADOW"}})
    assert "drift" not in ControllerParams().model_dump(mode="json")
    assert "drift" not in ControllerParams(evidence={}).model_dump(mode="json")  # type: ignore[arg-type]
    on = ControllerParams.model_validate({"evidence": {}, "drift": {"mode": "ENFORCE"}})
    assert on.model_dump(mode="json")["drift"]["mode"] == "ENFORCE"


# ---- simulation: the loop end to end ------------------------------------------------------------
def _cfg(stream, variant: str) -> SimConfig:  # type: ignore[no-untyped-def]
    from experience_engine.controller.drift_eval import controller_config

    return controller_config(variant, stream, DriftParams())


def test_in_the_loop_shadow_is_decision_for_decision_the_m61_controller() -> None:
    s = build_stream(SCENARIOS["S2"], 424242)
    order = list(range(s.scenario.n))
    a = run_stream(s.tasks, order, _cfg(s, "m61"), label="x").steps
    b = run_stream(s.tasks, order, _cfg(s, "m62_shadow"), label="x")
    assert [(x.chosen, x.status, x.tokens) for x in a] == [
        (x.chosen, x.status, x.tokens) for x in b.steps
    ]
    assert any(t.to_state == DriftState.DRIFTED for t in b.transitions)  # it still watched
    assert not any(t.applied for t in b.transitions)


def test_in_the_loop_enforce_falls_back_after_the_collapse_and_keeps_the_audit() -> None:
    s = build_stream(SCENARIOS["S2"], 424242)
    order = list(range(s.scenario.n))
    run = run_stream(s.tasks, order, _cfg(s, "m62"), label="x")
    quarantined = [
        x for x in run.steps if x.drift_state in ("DRIFTED", "QUARANTINED", "RECOVERING")
    ]
    assert quarantined and all(x.index >= 250 for x in quarantined)  # nothing before the drift
    assert all(x.chosen == PLANNING_KEY for x in run.steps[:250])  # the prior controlled until then
    late = [x for x in run.steps if x.index > 330 and x.drift_state == "QUARANTINED"]
    assert late and all(x.chosen == SIM_CAP_KEY for x in late)  # the rule default served
    assert all(t.applied and t.config_hash for t in run.transitions)
    shadow = run_stream(s.tasks, order, _cfg(s, "m61"), label="x").steps
    assert sum(x.success for x in run.steps) >= sum(
        x.success for x in shadow
    )  # fallback helps here


def test_a_stable_stream_is_not_disturbed_and_matches_m61_exactly() -> None:
    s = build_stream(SCENARIOS["S1"], 424243)
    order = list(range(s.scenario.n))
    a = run_stream(s.tasks, order, _cfg(s, "m61"), label="y").steps
    b = run_stream(s.tasks, order, _cfg(s, "m62"), label="y").steps
    assert [(x.chosen, x.status) for x in a] == [(x.chosen, x.status) for x in b]
