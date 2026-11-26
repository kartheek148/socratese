"""M6 unit tests: stage gates, canary routing, rollout wiring and the policy registry."""

import dataclasses
from typing import Any

import pytest

from controller_support import (
    CAP,
    CAP_KEY,
    PLANNING_KEY,
    MemoryPolicyStore,
    ctx,
    definition,
    match_with,
    record,
    snapshot_of,
    synthetic_log,
    task,
)
from experience_engine.controller import replay as replay_mod
from experience_engine.controller.gates import (
    ShadowPair,
    canary_assignment,
    evaluate_canary,
    evaluate_offline,
    evaluate_shadow,
)
from experience_engine.controller.policy import AdaptiveController, Choice
from experience_engine.controller.registry import (
    ControllerPolicyRegistry,
    ControllerRegistryError,
    IllegalPolicyTransitionError,
    PromotionBlockedError,
)
from experience_engine.controller.rollout import CanaryRouter, build_rollout
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.rollout import (
    PolicyState,
    RolloutGates,
    ShadowGates,
    Stage,
    StageReport,
)
from experience_engine.execution.controller import RuleCapabilityPolicy
from support import FakeClock

S = PolicyState
GOOD_CELLS = {PLANNING_KEY: (300, 270, 300 * 1000), CAP_KEY: (300, 255, 300 * 3000)}


def calibrated_candidate(version: int = 1, **kw: Any) -> Any:
    return definition(snapshot_of(GOOD_CELLS), version=version, **kw)


# ---- offline gate -------------------------------------------------------------------------------
def test_offline_passes_a_calibrated_candidate_that_is_safe_on_replay() -> None:
    logs = synthetic_log({CAP_KEY: 0.85}, {CAP_KEY: 3000}, 300, seed=2)
    rep = evaluate_offline(calibrated_candidate(), logs)
    assert rep.verdict == Verdict.PASS, rep.reasons
    assert rep.metrics["protected_deviations"] == 0 and rep.metrics["n_decisions"] == 300
    assert rep.gate_hash and rep.definition_hash == calibrated_candidate().definition_hash()


def test_offline_is_inconclusive_with_too_few_logged_decisions() -> None:
    rep = evaluate_offline(
        calibrated_candidate(), synthetic_log({CAP_KEY: 0.8}, {CAP_KEY: 100}, 20)
    )
    assert rep.verdict == Verdict.INCONCLUSIVE and "20 logged decisions" in rep.reasons[0]


def test_offline_fails_when_the_estimator_is_not_calibrated() -> None:
    bad = definition(snapshot_of(GOOD_CELLS, calibrated=False))
    rep = evaluate_offline(bad, synthetic_log({CAP_KEY: 0.8}, {CAP_KEY: 100}, 200))
    assert rep.verdict == Verdict.FAIL and "calibration" in " ".join(rep.reasons)


def test_offline_fails_on_predicted_harm_when_the_log_supports_the_candidate() -> None:
    # the candidate wrongly believes planning is as good and cheaper; the randomised log disagrees
    cand = calibrated_candidate()
    logs = synthetic_log(
        {CAP_KEY: 0.9, PLANNING_KEY: 0.2},
        {CAP_KEY: 3000, PLANNING_KEY: 1000},
        1000,
        seed=5,
        mix={CAP_KEY: 0.5, PLANNING_KEY: 0.5},
    )
    rep = evaluate_offline(cand, logs)
    assert rep.verdict == Verdict.FAIL and "below the logged" in " ".join(rep.reasons)
    assert rep.metrics["coverage"] == pytest.approx(1.0)


def test_offline_fails_on_protected_deviation_and_infeasible_choice(monkeypatch: Any) -> None:
    logs = [record(i, CAP_KEY, True, 100, risk="high") for i in range(150)]
    real = replay_mod.choose

    def broken(f: Any, params: Any) -> Choice:  # a buggy policy that ignores the protected class
        return Choice(PLANNING_KEY, 1.0, {PLANNING_KEY: 1.0}, {}, "bug", "GREEDY")

    monkeypatch.setattr(replay_mod, "choose", broken)
    rep = evaluate_offline(calibrated_candidate(), logs)
    assert rep.verdict == Verdict.FAIL and rep.metrics["protected_deviations"] == 150
    monkeypatch.setattr(replay_mod, "choose", real)

    def infeasible(f: Any, params: Any) -> Choice:
        return Choice(
            "DIRECT_CAPABILITY:ghost@1", 1.0, {"DIRECT_CAPABILITY:ghost@1": 1.0}, {}, "x", "G"
        )

    monkeypatch.setattr(replay_mod, "choose", infeasible)
    rep2 = evaluate_offline(
        calibrated_candidate(), [record(i, CAP_KEY, True, 1) for i in range(150)]
    )
    assert rep2.metrics["infeasible_choices"] == 150 and rep2.verdict == Verdict.FAIL


# ---- shadow gate --------------------------------------------------------------------------------
def _pairs(
    n: int, true_rate: float, believed: tuple[int, int, int], seed: int = 0
) -> list[ShadowPair]:
    import random

    rng = random.Random(seed)
    cells = {PLANNING_KEY: (100, 90, 100_000), CAP_KEY: believed}
    shadow = AdaptiveController(definition(snapshot_of(cells, calibrated=True)))
    pairs = []
    for i in range(n):
        d = shadow.decide(ctx(task(), match_with(CAP), f"s-{i}"))
        pairs.append(
            ShadowPair(
                record(i, CAP_KEY, rng.random() < true_rate, 3000),
                d.model_copy(update={"acted": False}),
            )
        )
    return pairs


def test_shadow_passes_with_calibrated_live_predictions() -> None:
    cand = definition(snapshot_of({PLANNING_KEY: (100, 90, 100_000), CAP_KEY: (300, 240, 900_000)}))
    rep = evaluate_shadow(cand, _pairs(300, 0.8, (300, 240, 900_000)))
    assert rep.verdict == Verdict.PASS, rep.reasons
    assert rep.metrics["n_scored_predictions"] == 300 and rep.metrics["live_ece"] is not None


def test_shadow_fails_on_live_miscalibration_and_on_errors() -> None:
    cand = definition(snapshot_of({PLANNING_KEY: (100, 90, 100_000), CAP_KEY: (300, 285, 900_000)}))
    bad = evaluate_shadow(cand, _pairs(300, 0.5, (300, 285, 900_000)))
    assert bad.verdict == Verdict.FAIL and "live ECE" in " ".join(bad.reasons)
    ok_pairs = _pairs(300, 0.95, (300, 285, 900_000))
    assert evaluate_shadow(cand, ok_pairs, decision_errors=2).verdict == Verdict.FAIL


def test_shadow_is_inconclusive_with_few_decisions() -> None:
    cand = definition(snapshot_of(GOOD_CELLS))
    rep = evaluate_shadow(
        cand,
        _pairs(10, 0.9, GOOD_CELLS[CAP_KEY]),
        gates=RolloutGates(shadow=ShadowGates(min_decisions=100)),
    )
    assert rep.verdict == Verdict.INCONCLUSIVE


# ---- canary assignment, router and gate ---------------------------------------------------------
def test_canary_assignment_is_stable_and_matches_the_fraction() -> None:
    ids = [f"task-{i}" for i in range(5000)]
    a = [canary_assignment(i, 0.2) for i in ids]
    assert a == [canary_assignment(i, 0.2) for i in ids]
    assert sum(a) / len(a) == pytest.approx(0.2, abs=0.02)
    assert a != [canary_assignment(i, 0.2, salt="other") for i in ids]


def test_router_never_exposes_protected_tasks_to_the_candidate() -> None:
    cand = AdaptiveController(calibrated_candidate(version=2))
    rule = RuleCapabilityPolicy()
    router = CanaryRouter(rule, cand, 0.5)
    seen_candidate = seen_control = 0
    for i in range(400):
        for risk in ("low", "medium", "high"):
            t = dataclasses.replace(
                ctx(task(risk), match_with(CAP), f"d{i}"),
                task=task(risk).model_copy(update={"task_id": f"k{i}"}),
            )
            d = router.decide(t)
            if d.policy_version == cand.policy_version:
                seen_candidate += 1
                assert risk == "low"
            else:
                seen_control += 1
    assert seen_candidate > 0 and seen_control > 0 and router.policy_version == rule.policy_version
    with pytest.raises(ValueError):
        CanaryRouter(rule, cand, 1.0)


def _canary_rows(cand_v: str, n_c: int, s_c: int, n_k: int, s_k: int) -> list[Any]:
    """Exact success counts (no sampling noise): s_c of n_c candidate runs succeed, etc."""
    rows = [
        record(i, CAP_KEY, i < s_c, 3000).model_copy(update={"policy_version": cand_v})
        for i in range(n_c)
    ]
    rows += [
        record(10_000 + i, CAP_KEY, i < s_k, 3000).model_copy(
            update={"policy_version": "rule-capability/1"}
        )
        for i in range(n_k)
    ]
    return rows


def test_canary_gate_pass_fail_inconclusive_and_protected_exposure() -> None:
    cand = calibrated_candidate(version=2)
    v = cand.policy_version
    ctl = "rule-capability/1"
    ok = evaluate_canary(cand, ctl, _canary_rows(v, 300, 240, 300, 240))
    assert ok.verdict == Verdict.PASS and ok.metrics["control_version"] is None  # rule control
    adaptive_ctl = evaluate_canary(
        cand,
        "adaptive/1",
        [
            r.model_copy(update={"policy_version": "adaptive/1"}) if r.policy_version == ctl else r
            for r in _canary_rows(v, 300, 240, 300, 240)
        ],
        control_adaptive_version="adaptive/1",
    )
    assert adaptive_ctl.verdict == Verdict.PASS
    assert adaptive_ctl.metrics["control_version"] == "adaptive/1"
    harm = evaluate_canary(cand, ctl, _canary_rows(v, 150, 75, 150, 128))
    assert harm.verdict == Verdict.FAIL and "significantly lower" in " ".join(harm.reasons)
    small = evaluate_canary(cand, ctl, _canary_rows(v, 10, 8, 300, 240))
    assert small.verdict == Verdict.INCONCLUSIVE
    wide = evaluate_canary(cand, ctl, _canary_rows(v, 40, 28, 40, 32))
    assert wide.verdict == Verdict.INCONCLUSIVE and "non-inferiority" in " ".join(wide.reasons)
    exposed = _canary_rows(v, 300, 240, 300, 240) + [
        record(99_999, CAP_KEY, True, 1, risk="high").model_copy(update={"policy_version": v})
    ]
    assert evaluate_canary(cand, ctl, exposed).verdict == Verdict.FAIL


# ---- registry -----------------------------------------------------------------------------------
class Rig:
    def __init__(self) -> None:
        self.store = MemoryPolicyStore()
        self.gates = RolloutGates()
        self.reg = ControllerPolicyRegistry(self.store, FakeClock(), self.gates)

    def evidence(
        self, d: Any, stage: Stage, verdict: Verdict = Verdict.PASS, control: str | None = None
    ) -> None:
        metrics = {"control_version": control} if stage == Stage.CANARY else {}
        self.reg.record_evaluation(
            d.tenant_id,
            StageReport(
                stage=stage,
                verdict=verdict,
                policy_version=d.policy_version,
                definition_hash=d.definition_hash(),
                gate_hash=self.gates.gate_hash(stage),
                metrics=metrics,
                reasons=[] if verdict == Verdict.PASS else ["nope"],
            ),
        )

    def state(self, d: Any) -> PolicyState:
        rec = self.store.get_version(d.tenant_id, d.policy_id, d.version)
        assert rec is not None
        return rec.state

    def promote(self, d: Any, to: PolicyState) -> None:
        self.reg.promote(d.tenant_id, d.policy_id, d.version, to, "tester", "test")

    def full_path(self, d: Any, control: str | None) -> None:
        self.evidence(d, Stage.OFFLINE)
        self.promote(d, S.SHADOW)
        self.evidence(d, Stage.SHADOW)
        self.promote(d, S.CANARY)
        self.evidence(d, Stage.CANARY, control=control)
        self.promote(d, S.ACTIVE)


def test_versions_are_sequential_and_immutable() -> None:
    r = Rig()
    r.reg.register(calibrated_candidate(1))
    with pytest.raises(ControllerRegistryError, match="must be 2"):
        r.reg.register(calibrated_candidate(1))
    with pytest.raises(ControllerRegistryError, match="must be 2"):
        r.reg.register(calibrated_candidate(3))
    r.reg.register(calibrated_candidate(2))


def test_every_forward_step_needs_a_pass_for_this_definition_and_gate_set() -> None:
    r = Rig()
    d1 = calibrated_candidate(1)
    r.reg.register(d1)
    with pytest.raises(PromotionBlockedError, match="no OFFLINE evaluation"):
        r.promote(d1, S.SHADOW)
    r.evidence(d1, Stage.OFFLINE, Verdict.INCONCLUSIVE)
    with pytest.raises(PromotionBlockedError, match="INCONCLUSIVE"):
        r.promote(d1, S.SHADOW)
    r.evidence(d1, Stage.OFFLINE, Verdict.FAIL)
    with pytest.raises(PromotionBlockedError, match="FAIL"):
        r.promote(d1, S.SHADOW)
    r.evidence(d1, Stage.OFFLINE)
    with pytest.raises(IllegalPolicyTransitionError):
        r.promote(d1, S.ACTIVE)  # cannot skip stages
    r.promote(d1, S.SHADOW)
    # gate-set drift invalidates old evidence
    r.evidence(d1, Stage.SHADOW)
    r.reg.gates = RolloutGates(shadow=ShadowGates(min_decisions=7))
    with pytest.raises(PromotionBlockedError, match="different gate set"):
        r.promote(d1, S.CANARY)


def test_evidence_for_a_different_definition_is_refused() -> None:
    r = Rig()
    d1 = calibrated_candidate(1)
    r.reg.register(d1)
    other = definition(snapshot_of({PLANNING_KEY: (1, 1, 1)}), version=1)
    with pytest.raises(ControllerRegistryError, match="different definition"):
        r.evidence(other, Stage.OFFLINE)


def test_full_lifecycle_supersession_and_rollback_restore() -> None:
    r = Rig()
    d1, d2 = calibrated_candidate(1), calibrated_candidate(2)
    r.reg.register(d1)
    r.reg.register(d2)
    r.full_path(d1, control=None)  # no previous ACTIVE: the control was the rule baseline
    assert r.state(d1) == S.ACTIVE
    r.evidence(d2, Stage.OFFLINE)
    r.promote(d2, S.SHADOW)
    r.evidence(d2, Stage.SHADOW)
    r.promote(d2, S.CANARY)
    r.evidence(d2, Stage.CANARY, control="adaptive/9")  # evidence against a different control
    with pytest.raises(PromotionBlockedError, match="canary control"):
        r.promote(d2, S.ACTIVE)
    r.evidence(d2, Stage.CANARY, control=d1.policy_version)
    r.promote(d2, S.ACTIVE)
    assert (r.state(d1), r.state(d2)) == (S.SUPERSEDED, S.ACTIVE)
    r.reg.rollback(d2.tenant_id, d2.policy_id, 2, "ops", "regression")
    assert (r.state(d1), r.state(d2)) == (S.ACTIVE, S.ROLLED_BACK)
    hist = [
        (h.version, h.from_state, h.to_state) for h in r.store.history(d1.tenant_id, d1.policy_id)
    ]
    assert (1, S.ACTIVE, S.SUPERSEDED) in hist and (1, S.SUPERSEDED, S.ACTIVE) in hist
    with pytest.raises(IllegalPolicyTransitionError):
        r.reg.rollback(d2.tenant_id, d2.policy_id, 2, "ops", "again")  # terminal
    with pytest.raises(IllegalPolicyTransitionError):
        r.promote(d2, S.SHADOW)


def test_one_shadow_and_one_canary_at_a_time_and_reject_is_terminal() -> None:
    r = Rig()
    ds = [calibrated_candidate(i) for i in (1, 2)]
    for d in ds:
        r.reg.register(d)
        r.evidence(d, Stage.OFFLINE)
    r.promote(ds[0], S.SHADOW)
    with pytest.raises(PromotionBlockedError, match="already in SHADOW"):
        r.promote(ds[1], S.SHADOW)
    r.reg.reject(ds[1].tenant_id, ds[1].policy_id, 2, "ops", "no")
    assert r.state(ds[1]) == S.REJECTED
    with pytest.raises(IllegalPolicyTransitionError):
        r.promote(ds[1], S.SHADOW)


def test_canary_guard_rolls_back_automatically_on_a_fail() -> None:
    r = Rig()
    d = calibrated_candidate(1)
    r.reg.register(d)
    r.evidence(d, Stage.OFFLINE)
    r.promote(d, S.SHADOW)
    r.evidence(d, Stage.SHADOW)
    r.promote(d, S.CANARY)
    failing = StageReport(
        stage=Stage.CANARY,
        verdict=Verdict.FAIL,
        policy_version=d.policy_version,
        definition_hash=d.definition_hash(),
        gate_hash=r.gates.gate_hash(Stage.CANARY),
        reasons=["harm"],
    )
    assert r.reg.apply_canary_guard(d.tenant_id, d.policy_id, 1, failing) is True
    assert r.state(d) == S.ROLLED_BACK
    assert r.store.history(d.tenant_id, d.policy_id)[-1].actor == "canary-guard"


# ---- rollout wiring -----------------------------------------------------------------------------
def test_build_rollout_reflects_registry_states_and_rollbacks_take_effect() -> None:
    r = Rig()
    d1, d2, d3 = (calibrated_candidate(i) for i in (1, 2, 3))
    for d in (d1, d2, d3):
        r.reg.register(d)
    rule = RuleCapabilityPolicy()
    empty = build_rollout(r.store, "bench", "adaptive", rule)
    assert empty.controller is rule and empty.shadow is None and empty.active_version is None
    r.full_path(d1, control=None)
    r.evidence(d2, Stage.OFFLINE)
    r.promote(d2, S.SHADOW)
    r.evidence(d3, Stage.OFFLINE)  # d3 stays candidate
    s = build_rollout(r.store, "bench", "adaptive", rule)
    assert (
        s.active_version == "adaptive/1"
        and s.shadow_version == "adaptive/2"
        and s.canary_version is None
    )
    assert s.controller.policy_version == "adaptive/1" and s.shadow is not None
    r.evidence(d2, Stage.SHADOW)
    r.promote(d2, S.CANARY)
    c = build_rollout(r.store, "bench", "adaptive", rule)
    assert isinstance(c.controller, CanaryRouter) and c.canary_version == "adaptive/2"
    r.reg.rollback("bench", "adaptive", 1, "ops", "bad")
    after = build_rollout(r.store, "bench", "adaptive", rule)
    # no previously superseded version exists: the rule baseline serves, the canary keeps splitting
    assert after.active_version is None and isinstance(after.controller, CanaryRouter)
    assert after.controller.policy_version == rule.policy_version
    other_tenant = build_rollout(r.store, "someone-else", "adaptive", rule)
    assert other_tenant.controller is rule and other_tenant.canary_version is None
