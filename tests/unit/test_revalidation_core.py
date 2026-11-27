"""M6.3 revalidation core: exact statistics, reasons, eligibility, the case machine, the fleet
composed with the (unchanged) M6.2 monitor, lineage, budgets, replay."""

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from test_drift_monitor import ALT, DEF, T0, P, rec, ref

from experience_engine.controller.drift import CellReference, MonitorFleet
from experience_engine.controller.revalidation import (
    CaseState,
    ReplaySource,
    RevalidationFleet,
    binom_cdf,
    binom_sf,
    check_eligibility,
    classify_run_outcome,
    critical_k,
    hypotheses,
    plan_fixed,
    reason_and_severity,
    reason_of,
    replay_revalidation,
    required_dwell,
    sprt_bounds,
    sprt_step,
    step_case,
    strategy_ready,
)
from experience_engine.domain.controller import paired_key
from experience_engine.domain.drift import (
    Detector,
    DriftMode,
    DriftState,
    DriftTransition,
    DriftTrigger,
)
from experience_engine.domain.outcome import OutcomeStatus, VerificationResult
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    QuarantineReason,
    RejectReason,
    RevalidationParams,
    RevalState,
    RevalStrategy,
    RevalTransition,
    RevalTrial,
    RevalTrigger,
    TrialResult,
    TrialVerdict,
)
from revalidation_support import Script

CELL = paired_key("timeout", "low", DEF, ALT)
S, F, U = TrialVerdict.SUCCESS, TrialVerdict.FAILURE, TrialVerdict.VERIFICATION_UNAVAILABLE


def rp(strategy: RevalStrategy = RevalStrategy.FIXED, **kw: Any) -> RevalidationParams:
    base: dict[str, Any] = {"dwell": 0}
    return RevalidationParams(strategy=strategy, **{**base, **kw})


def hours(h: float) -> timedelta:
    return timedelta(hours=h)


START = T0 + hours(5)


def open_case(
    reason: QuarantineReason = QuarantineReason.PERFORMANCE_DEGRADATION,
    severity: float = 0.0,
    **kw: Any,
) -> CaseState:
    return CaseState(
        cell_key=CELL,
        episode=1,
        reason=reason,
        severity=severity,
        trigger_decision_id="d-0",
        trigger_at=T0,
        opened_at=T0,
        **kw,
    )


def drive(
    case: CaseState,
    params: RevalidationParams,
    source: Script,
    rounds: int,
    *,
    r: CellReference | None = None,
    execute: bool = True,
    spent: int = 0,
    start: datetime = START,
    step: float = 10.0,
    tenant: str = "bench",
) -> tuple[CaseState, list[RevalTransition], list[RevalTrial], int]:
    r = r or ref(0.5)
    ts: list[RevalTransition] = []
    trials: list[RevalTrial] = []
    for k in range(rounds):
        now = start + hours(step * k)
        res = step_case(
            r, params, case, execute=execute, tenant_id=tenant, source=source,
            facts=source.facts(r, now) if execute else None, lifetime_spent=spent,
            anchor=f"a{k}", now=now, applied=execute,
        )  # fmt: skip
        case, spent = res.case, spent + res.tokens
        ts += res.transitions
        trials += res.trials
    return case, ts, trials, spent


def triggers(ts: list[RevalTransition]) -> list[str]:
    return [t.trigger.value for t in ts]


# ---- exact statistics ---------------------------------------------------------------------------
def test_binomial_tails_match_brute_force() -> None:
    n, p = 12, 0.37
    pmf = [math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(n + 1)]
    for k in range(n + 2):
        assert binom_sf(k, n, p) == pytest.approx(sum(pmf[k:]), abs=1e-12)
        assert binom_cdf(k - 1, n, p) == pytest.approx(sum(pmf[:k]), abs=1e-12)


@pytest.mark.parametrize("n,p_bad,alpha", [(30, 0.4, 0.05), (160, 0.42, 0.05), (200, 0.45, 0.025)])
def test_critical_value_is_the_smallest_with_exact_size(n: int, p_bad: float, alpha: float) -> None:
    k = critical_k(n, p_bad, alpha)
    assert binom_sf(k, n, p_bad) <= alpha < binom_sf(k - 1, n, p_bad)


def test_fixed_plan_has_exact_size_and_power() -> None:
    plan = plan_fixed(0.42, 0.52, 0.05, 0.20, 320)
    assert plan is not None
    assert plan.size <= 0.05 and plan.power >= 0.80 and 100 < plan.n < 320
    tight = plan_fixed(0.42, 0.52, 0.025, 0.20, 320)
    assert tight is not None and tight.n > plan.n  # a stricter error rate needs more evidence
    assert plan_fixed(0.42, 0.52, 0.05, 0.20, 50) is None  # cannot be planned within the cap
    assert plan_fixed(0.5, 0.5, 0.05, 0.20, 320) is None  # no margin


def test_sequential_test_controls_both_errors() -> None:
    p_bad, p_good, alpha, beta = 0.40, 0.50, 0.05, 0.20
    lo, hi = sprt_bounds(alpha, beta)
    assert lo < 0 < hi

    def accepts(p: float, seed: int) -> bool:
        rng = random.Random(seed)
        llr = 0.0
        for _ in range(2000):
            llr += sprt_step(p_bad, p_good, rng.random() < p)
            if llr >= hi:
                return True
            if llr <= lo:
                return False
        return False

    n = 1500
    false_accept = sum(accepts(p_bad, s) for s in range(n)) / n
    true_accept = sum(accepts(p_good, 10_000 + s) for s in range(n)) / n
    assert false_accept <= alpha + 0.015  # Wald's bounds, Monte Carlo tolerance
    assert true_accept >= 1 - beta - 0.04


def test_hypotheses_are_clamped_to_valid_probabilities() -> None:
    p_bad, p_good = hypotheses(ref(0.05), 0.10)
    assert 0.0 < p_bad < p_good < 1.0


# ---- reasons ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "trigger,detector,expected",
    [
        (
            DriftTrigger.SUCCESS_DEGRADATION,
            Detector.WINDOW,
            QuarantineReason.PERFORMANCE_DEGRADATION,
        ),
        (DriftTrigger.SUCCESS_DEGRADATION, Detector.CUSUM, QuarantineReason.STATISTICAL_DRIFT),
        (DriftTrigger.COST_DEGRADATION, Detector.WINDOW, QuarantineReason.COST_DEGRADATION),
        (DriftTrigger.UTILITY_DEGRADATION, Detector.WINDOW, QuarantineReason.COST_DEGRADATION),
        (DriftTrigger.CONTEXT_MISMATCH, Detector.WINDOW, QuarantineReason.STRUCTURAL_INVALIDATION),
        (DriftTrigger.EVIDENCE_STALE, Detector.WINDOW, QuarantineReason.EVIDENCE_STALE),
        (DriftTrigger.SIGNAL_CLEARED, Detector.WINDOW, QuarantineReason.UNKNOWN),
        (DriftTrigger.RECOVERY_FAILED, Detector.WINDOW, QuarantineReason.UNKNOWN),
    ],
)
def test_reason_is_a_projection_of_the_existing_trigger(
    trigger: DriftTrigger, detector: Detector, expected: QuarantineReason
) -> None:
    assert reason_of(trigger, detector) == expected


def _dt(seq: int, to: DriftState, trig: DriftTrigger, meas: dict[str, float] | None = None,
        decision: str = "d-9") -> DriftTransition:  # fmt: skip
    return DriftTransition(
        cell_key=CELL, seq=seq, from_state=DriftState.NORMAL, to_state=to, trigger=trig,
        effect=DriftTransition.model_fields["effect"].default or "MONITOR",  # type: ignore[arg-type]
        measurements=meas or {}, n_window=40, decision_id=decision, config_hash="h",
    )  # fmt: skip


def test_reason_and_severity_come_from_the_drifted_transition_before_the_quarantine() -> None:
    from experience_engine.domain.drift import DriftEffect

    def tr(seq: int, to: DriftState, trig: DriftTrigger, meas: dict[str, float], d: str = "d-9"):
        return DriftTransition(
            cell_key=CELL, seq=seq, from_state=DriftState.SUSPECT, to_state=to, trigger=trig,
            effect=DriftEffect.FALLBACK_TO_RULE, measurements=meas, n_window=40, decision_id=d,
            config_hash="h",
        )  # fmt: skip

    drifted = tr(3, DriftState.DRIFTED, DriftTrigger.SUCCESS_DEGRADATION,
                 {"success_rate": 0.2, "ref_success": 0.5})  # fmt: skip
    quarantined = tr(4, DriftState.QUARANTINED, DriftTrigger.QUARANTINE_APPLIED, {})
    reason, severity = reason_and_severity(quarantined, [drifted, quarantined], Detector.WINDOW)
    assert reason == QuarantineReason.PERFORMANCE_DEGRADATION and severity == pytest.approx(0.3)
    other = tr(3, DriftState.DRIFTED, DriftTrigger.CONTEXT_MISMATCH, {}, d="d-1")
    assert reason_and_severity(quarantined, [other, quarantined], Detector.WINDOW) == (
        QuarantineReason.UNKNOWN,
        0.0,
    )  # a different decision is not the cause
    assert (
        reason_and_severity(quarantined, [quarantined], Detector.WINDOW)[0]
        == QuarantineReason.UNKNOWN
    )


# ---- independent verification -------------------------------------------------------------------
def vr(passed: bool | None, role: str = "goal") -> VerificationResult:
    return VerificationResult(criterion_kind="x", role=role, passed=passed, detail="d")  # type: ignore[arg-type]


def test_only_an_explicitly_verified_pass_is_a_success() -> None:
    ok = OutcomeStatus.VERIFIED_SUCCESS
    assert classify_run_outcome(ok, [vr(True)]) == TrialVerdict.SUCCESS
    assert classify_run_outcome(ok, [vr(True), vr(None)]) == TrialVerdict.VERIFICATION_UNAVAILABLE
    assert classify_run_outcome(ok, []) == TrialVerdict.FAILURE  # no goal evidence: not a success
    assert classify_run_outcome(ok, [vr(True), vr(False)]) == TrialVerdict.FAILURE  # every goal
    assert classify_run_outcome(OutcomeStatus.UNKNOWN, [vr(True)]) == U
    assert classify_run_outcome(OutcomeStatus.PARTIAL, [vr(True)]) == TrialVerdict.FAILURE
    assert classify_run_outcome(ok, [vr(True)], timed_out=True) == TrialVerdict.TIMEOUT
    assert classify_run_outcome(ok, [vr(True)], invalid_output=True) == TrialVerdict.INVALID_OUTPUT
    assert classify_run_outcome(OutcomeStatus.FAILURE, [vr(False)]) == TrialVerdict.FAILURE
    assert classify_run_outcome(OutcomeStatus.FAILURE, [vr(True)]) == TrialVerdict.FAILURE


def test_unavailable_trials_are_never_successes_and_can_never_recover() -> None:
    case, ts, trials, _ = drive(open_case(), rp(), Script(U), 12)
    assert case.state != RevalState.RECOVERED
    assert case.cycle >= 1 or case.state == RevalState.BUDGET_EXHAUSTED
    assert all(t.result.verdict == U for t in trials)
    assert RevalTrigger.VERIFICATION_UNAVAILABLE.value in triggers(ts)
    assert all(t.measurements.get("n", 0.0) == 0.0 for t in ts)  # none was counted as evidence
    mixed = Script(lambda n: U if n % 3 == 0 else S)  # a third unavailable: > 20 %
    case2, ts2, _, _ = drive(open_case(), rp(), mixed, 12)
    assert RevalState.RECOVERED not in {t.to_state for t in ts2}
    assert RevalTrigger.VERIFICATION_UNAVAILABLE.value in triggers(ts2)


# ---- eligibility --------------------------------------------------------------------------------
GOOD = ExecutorFacts(
    tenant_id="bench", runtime_version="rt-1", verifier_version="v-1", pool=500,
)  # fmt: skip


def elig(
    case: CaseState,
    facts: ExecutorFacts | None = GOOD,
    spent: int = 0,
    r: CellReference | None = None,
    params: RevalidationParams | None = None,
    tenant: str = "bench",
):  # type: ignore[no-untyped-def]
    return check_eligibility(params or rp(), case, r or ref(0.5), tenant_id=tenant,
                             facts=facts, lifetime_spent=spent)  # fmt: skip


def test_a_healthy_case_with_a_capable_executor_is_eligible() -> None:
    e = elig(open_case())
    assert e.eligible and not e.reasons and not e.permanent


@pytest.mark.parametrize(
    "mutate,gate,permanent",
    [
        (lambda: dict(case=open_case(QuarantineReason.UNKNOWN)), "reason_known", True),
        (lambda: dict(facts=GOOD.model_copy(update={"tenant_id": "other"})), "tenant", True),
        (lambda: dict(facts=GOOD.model_copy(update={"supported": False})), "identity", False),
        (
            lambda: dict(facts=GOOD.model_copy(update={"runtime_version": None})),
            "versions_known",
            False,
        ),
        (
            lambda: dict(facts=GOOD.model_copy(update={"verifier_version": None})),
            "versions_known",
            False,
        ),
        (
            lambda: dict(facts=GOOD.model_copy(update={"verification_available": False})),
            "verification_available",
            False,
        ),
        (lambda: dict(facts=GOOD.model_copy(update={"pool": 3})), "pool", False),
        (lambda: dict(spent=2_990_000), "budget", True),
        (lambda: dict(case=open_case(cycle=3)), "cycles", True),
        (
            lambda: dict(
                r=ref(0.5, family="timeout").__class__(**{**ref(0.5).__dict__, "risk": "high"})
            ),
            "risk",
            True,
        ),
        (lambda: dict(params=rp(max_trials=20)), "plan", True),
    ],
)
def test_each_gate_blocks_on_its_own_and_says_so(mutate, gate: str, permanent: bool) -> None:  # type: ignore[no-untyped-def]
    e = elig(**{**dict(case=open_case()), **mutate()})
    assert not e.eligible and any(r.startswith(gate) for r in e.reasons), e.reasons
    assert e.permanent is permanent


def test_a_verification_failure_quarantine_needs_working_verification() -> None:
    case = open_case(QuarantineReason.VERIFICATION_FAILURE)
    assert not elig(case, GOOD.model_copy(update={"verification_available": False})).eligible
    assert elig(case, GOOD).eligible


def test_without_an_executor_the_executor_gates_are_not_evaluated() -> None:
    assert elig(open_case(), facts=None).eligible  # shadow mode: nothing to ask


# ---- readiness (strategies) ---------------------------------------------------------------------
def test_dwell_is_doubled_for_severe_failures_and_every_strategy_needs_it() -> None:
    p = rp(RevalStrategy.SEQUENTIAL, dwell=50)
    mild, severe = open_case(severity=0.1), open_case(severity=0.3)
    assert required_dwell(p, mild) == 50 and required_dwell(p, severe) == 100
    assert not strategy_ready(p, replace(mild, dwell=49), ref(0.5))[0]
    assert strategy_ready(p, replace(mild, dwell=50), ref(0.5))[0]
    assert not strategy_ready(p, replace(severe, dwell=99), ref(0.5))[0]


def test_the_evidence_strategy_also_waits_for_a_healthy_default_arm() -> None:
    p = rp(RevalStrategy.EVIDENCE_THRESHOLD, dwell=50)
    r = ref(0.5, 0.5)
    few = open_case(dwell=60, default_n=10, default_k=5)
    assert not strategy_ready(p, few, r)[0]  # < ready_default_min verified outcomes
    healthy = open_case(dwell=60, default_n=60, default_k=30)
    assert strategy_ready(p, healthy, r)[0]
    degraded = open_case(dwell=60, default_n=60, default_k=12)  # 20 % against a 50 % reference
    ok, meas = strategy_ready(p, degraded, r)
    assert not ok and meas["default_success_diff_ub"] < -0.10
    assert strategy_ready(rp(RevalStrategy.FIXED, dwell=50), degraded, r)[0]  # A does not look


# ---- the case machine: lifecycle ----------------------------------------------------------------
def test_fixed_recovery_goes_through_every_state_and_is_explainable() -> None:
    p = rp(RevalStrategy.FIXED)
    case, ts, trials, spent = drive(open_case(), p, Script(S), 12)
    assert case.state == RevalState.RECOVERED
    assert triggers(ts) == ["READY", "TRIALS_STARTED", "CRITERIA_MET", "CONFIRMED"]
    assert [t.to_state.value for t in ts] == [
        "REVALIDATION_PENDING", "REVALIDATING", "RECOVERY_CANDIDATE", "RECOVERED",
    ]  # fmt: skip
    done = ts[-1]
    assert done.params_hash == p.config_hash() and done.strategy == RevalStrategy.FIXED
    for key in ("n", "k", "plan_n", "k_crit", "alpha", "p_ref", "confirm_n", "tokens_cycle",
                "first_ordinal", "last_ordinal"):  # fmt: skip
        assert key in done.measurements
    assert done.measurements["n"] == done.measurements["plan_n"]  # no peeking: the whole N
    assert spent == sum(t.result.tokens for t in trials)
    assert {t.key.stage.value for t in trials} == {"EVIDENCE", "CONFIRMATION"}
    ordinals = [t.key.ordinal for t in trials if t.key.stage.value == "EVIDENCE"]
    assert ordinals == list(range(len(ordinals)))  # contiguous: every trial is accounted for


def test_a_candidate_is_not_recovered_until_the_gap_and_confirmation_pass() -> None:
    p = rp(RevalStrategy.FIXED, confirm_gap_hours=30.0)
    case, ts, _, _ = drive(open_case(), p, Script(S), 5, step=10)  # evidence done, still waiting
    assert case.state == RevalState.RECOVERY_CANDIDATE
    assert RevalState.RECOVERED not in {t.to_state for t in ts}
    case2, ts2, _, _ = drive(case, p, Script(S), 8, start=T0 + hours(80))
    assert case2.state == RevalState.RECOVERED


def test_confirmation_is_a_collapse_guard_that_can_return_the_case_to_quarantine() -> None:
    p = rp(RevalStrategy.FIXED)
    n_evidence = {"n": 0}

    def verdict(i: int) -> TrialVerdict:
        return S if i < 400 and n_evidence.update(n=i) is None and i < plan_n else F

    plan_n = plan_fixed(*hypotheses(ref(0.5), 0.10), 0.05, 0.20, 320).n  # type: ignore[union-attr]
    case, ts, _, _ = drive(open_case(), p, Script(verdict), 14)
    assert "CRITERIA_MET" in triggers(ts) and "CONFIRMATION_FAILED" in triggers(ts)
    assert case.state in (RevalState.QUARANTINED, RevalState.REVALIDATION_PENDING,
                          RevalState.REVALIDATING) and case.cycle >= 1  # fmt: skip
    assert RevalState.RECOVERED not in {t.to_state for t in ts}


def test_confirmation_can_be_switched_off_and_then_restores_at_once() -> None:
    p = rp(RevalStrategy.FIXED, confirm_trials=0)
    _, ts, trials, _ = drive(open_case(), p, Script(S), 8)
    assert triggers(ts)[-2:] == ["CRITERIA_MET", "CONFIRMED"]
    assert all(t.key.stage.value == "EVIDENCE" for t in trials)


def test_the_sequential_strategy_stops_early_in_both_directions() -> None:
    p = rp(RevalStrategy.SEQUENTIAL, confirm_trials=0)
    dead, ts_dead, trials_dead, _ = drive(open_case(), p, Script(F), 1)
    assert "CRITERIA_NOT_MET" in triggers(ts_dead) and len(trials_dead) < 15
    healthy, ts_ok, trials_ok, _ = drive(open_case(), p, Script(S), 6)
    assert healthy.state == RevalState.RECOVERED
    fixed_n = plan_fixed(*hypotheses(ref(0.5), 0.10), 0.05, 0.20, 320).n  # type: ignore[union-attr]
    assert len(trials_ok) < fixed_n  # all successes: far fewer trials than the fixed sample


def test_repeated_failures_exhaust_the_cycles_and_end_the_episode() -> None:
    p = rp(RevalStrategy.SEQUENTIAL)
    case, ts, _, spent = drive(open_case(), p, Script(F), 12)
    assert case.state == RevalState.BUDGET_EXHAUSTED
    assert triggers(ts).count("CRITERIA_NOT_MET") == 2 and "CYCLES_EXHAUSTED" in triggers(ts)
    assert case.cycle == 3
    again, more, _, _ = drive(case, p, Script(S), 5)  # terminal: nothing ever restarts it
    assert more == [] and again.state == RevalState.BUDGET_EXHAUSTED


def test_a_failed_cycle_resets_the_cooldown_and_default_evidence() -> None:
    p = rp(RevalStrategy.SEQUENTIAL, dwell=30)
    case = open_case(dwell=30, default_n=30, default_k=15)
    case, ts, _, _ = drive(case, p, Script(F), 1)
    assert case.state == RevalState.QUARANTINED and case.cycle == 1
    assert case.dwell == 0 and case.default_n == 0 and case.n == 0 and case.ordinal == 0
    case, ts2, _, _ = drive(case, p, Script(S), 3)  # not ready again until 30 more decisions
    assert ts2 == [] and case.state == RevalState.QUARANTINED


def test_no_recovery_from_time_alone() -> None:
    rng = random.Random(3)
    for strategy in RevalStrategy:
        p = rp(strategy, dwell=5)
        for source in (Script(F), Script(U), Script(lambda n: F if rng.random() < 0.9 else S)):
            case = open_case(dwell=500)
            for k in range(60):  # arbitrary, including enormous, gaps between rounds
                now = T0 + hours(rng.choice([1, 10, 100, 10_000]) * (k + 1))
                res = step_case(ref(0.5), p, case, execute=True, tenant_id="bench", source=source,
                                facts=source.facts(ref(0.5), now), lifetime_spent=0, anchor=f"a{k}",
                                now=now)  # fmt: skip
                case = res.case
                assert case.state != RevalState.RECOVERED
    idle = Script(lambda n: S)
    idle.run = lambda *a, **k: None  # type: ignore[method-assign,assignment]
    case, ts, _, _ = drive(open_case(), rp(), idle, 30)  # an executor that never answers
    assert case.state != RevalState.RECOVERED


# ---- verdict semantics and the exact decision boundary ------------------------------------------
@pytest.mark.parametrize(
    "verdict", [TrialVerdict.TIMEOUT, TrialVerdict.INVALID_OUTPUT, TrialVerdict.FAILURE]
)
def test_timeouts_and_invalid_outputs_are_counted_failures_never_successes(
    verdict: TrialVerdict,
) -> None:
    assert not TrialResult(verdict=verdict, tokens=1, task_ref="x", observed_at=T0).success
    assert TrialResult(verdict=verdict, tokens=1, task_ref="x", observed_at=T0).counted
    assert not TrialResult(verdict=U, tokens=1, task_ref="x", observed_at=T0).counted
    _, ts, _, _ = drive(open_case(), rp(RevalStrategy.SEQUENTIAL), Script(verdict), 1)
    failed = next(t for t in ts if t.trigger == RevalTrigger.CRITERIA_NOT_MET)
    assert failed.measurements["k"] == 0 and failed.measurements["n"] >= 5  # in n, not in k


def test_the_fixed_test_recovers_at_exactly_the_critical_value_and_not_one_below() -> None:
    plan = plan_fixed(*hypotheses(ref(0.5), 0.10), 0.05, 0.20, 320)
    assert plan is not None

    def outcome(successes: int) -> list[str]:
        script = Script(lambda n: S if n < successes else F)
        _, ts, _, _ = drive(open_case(), rp(RevalStrategy.FIXED, confirm_trials=0), script, 8)
        return triggers(ts)

    assert "CRITERIA_MET" in outcome(plan.k_crit)
    below = outcome(plan.k_crit - 1)
    assert "CRITERIA_NOT_MET" in below and "CRITERIA_MET" not in below


def test_the_final_cost_guard_vetoes_a_pass_that_futility_could_not_see() -> None:
    noisy = Script(S, tokens=lambda n: 2_000 if n % 2 else 12_000)  # mean 7,000: +16.7 %
    _, ts, trials, _ = drive(open_case(), rp(RevalStrategy.FIXED), noisy, 8)
    fail = next(t for t in ts if t.trigger == RevalTrigger.CRITERIA_NOT_MET)
    assert fail.detail == ["cost guard failed"] and fail.measurements["k"] == fail.measurements["n"]
    assert RevalState.RECOVERED not in {t.to_state for t in ts}
    fine = Script(S, tokens=lambda n: 4_000 if n % 2 else 8_000)  # mean 6,000: on the reference
    done, _, _, _ = drive(open_case(), rp(RevalStrategy.FIXED), fine, 12)
    assert done.state == RevalState.RECOVERED


# ---- lineage ------------------------------------------------------------------------------------
def test_trials_that_break_lineage_are_rejected_recorded_and_not_counted() -> None:
    over = {
        0: {"observed_at": T0},  # not newer than the evidence that triggered quarantine
        1: {"tenant_id": "other"},
        3: {"task_ref": "t2"},  # reuse of a task
        5: {"runtime_version": "rt-9"},  # not the reference's version
    }
    case, ts, trials, _ = drive(open_case(), rp(RevalStrategy.SEQUENTIAL), Script(S, over=over), 1)
    why = {t.key.ordinal: t.reject_reason for t in trials[:7]}
    assert why[0] == RejectReason.NOT_NEWER and why[1] == RejectReason.TENANT_MISMATCH
    assert why[3] == RejectReason.DUPLICATE and why[5] == RejectReason.VERSION_MISMATCH
    assert why[2] is None and why[4] is None and why[6] is None
    assert all(not t.accepted for t in trials[:6] if t.reject_reason)
    counted = [t for t in trials if t.accepted and t.key.stage.value == "EVIDENCE"]
    assert case.n_rejected == 4 and case.n == len(counted)
    assert case.tokens_cycle == sum(t.result.tokens for t in trials)  # rejected trials still cost


def test_repeated_version_mismatch_reclassifies_the_case_as_structural() -> None:
    p = rp(RevalStrategy.SEQUENTIAL, lineage_mismatch_limit=5)
    case, ts, _, _ = drive(open_case(), p, Script(S, runtime="rt-2"), 1)
    assert "LINEAGE_VIOLATION" in triggers(ts)
    assert case.reason == QuarantineReason.STRUCTURAL_INVALIDATION and case.cycle == 1


def test_a_structural_case_validates_under_the_new_version_and_rebases_the_reference() -> None:
    p = rp(RevalStrategy.FIXED)
    case = open_case(QuarantineReason.STRUCTURAL_INVALIDATION)
    done, ts, trials, _ = drive(case, p, Script(S, runtime="rt-2"), 12)
    assert done.state == RevalState.RECOVERED
    assert ts[-1].rebase == {"runtime_version": "rt-2", "verifier_version": "v-1"}
    assert all(t.accepted for t in trials)


def test_a_structural_case_rejects_trials_that_mix_versions() -> None:
    over = {4: {"runtime_version": "rt-3"}}
    case = open_case(QuarantineReason.STRUCTURAL_INVALIDATION)
    _, _, trials, _ = drive(case, rp(RevalStrategy.FIXED), Script(S, runtime="rt-2", over=over), 1)
    assert trials[4].reject_reason == RejectReason.VERSION_MISMATCH
    assert all(t.accepted for i, t in enumerate(trials[:8]) if i != 4)


def test_a_version_change_alone_is_never_evidence_of_recovery() -> None:
    p = rp(RevalStrategy.SEQUENTIAL)
    case = open_case(QuarantineReason.STRUCTURAL_INVALIDATION)
    done, ts, _, _ = drive(case, p, Script(F, runtime="rt-2"), 6)  # new version, capability broken
    assert done.state != RevalState.RECOVERED and "CRITERIA_NOT_MET" in triggers(ts)


def test_a_stale_case_refreshes_the_evidence_time_on_recovery() -> None:
    case = open_case(QuarantineReason.EVIDENCE_STALE)
    done, ts, trials, _ = drive(case, rp(RevalStrategy.FIXED), Script(S), 12)
    assert done.state == RevalState.RECOVERED
    stamp = datetime.fromisoformat(ts[-1].rebase["recorded_at"])
    assert stamp == max(t.result.observed_at for t in trials if t.accepted)


# ---- budgets and expiry -------------------------------------------------------------------------
def test_the_trial_cap_ends_an_inconclusive_sequential_cycle() -> None:
    p = rp(RevalStrategy.SEQUENTIAL)
    p_bad, p_good = hypotheses(ref(0.5), 0.10)
    state = {"llr": 0.0}

    def hover(_: int) -> TrialVerdict:  # keeps the log-likelihood ratio inside the boundaries
        ok = state["llr"] < 0.2
        state["llr"] += sprt_step(p_bad, p_good, ok)
        return S if ok else F

    case, ts, trials, _ = drive(open_case(), p, Script(hover), 12)
    assert case.state == RevalState.BUDGET_EXHAUSTED and "TRIAL_CAP" in triggers(ts)
    assert len([t for t in trials if t.key.stage.value == "EVIDENCE"]) == p.max_trials


def test_the_token_cap_bounds_a_cycle() -> None:
    p = rp(RevalStrategy.FIXED, cost_guard=100.0)
    case, ts, trials, spent = drive(open_case(), p, Script(S, tokens=100_000), 12)
    assert case.state == RevalState.BUDGET_EXHAUSTED and "TOKEN_CAP" in triggers(ts)
    cap = int(1.5 * p.max_trials * ref(0.5).mean_tokens)
    assert cap <= spent < cap + 100_000


def test_the_lifetime_budget_bounds_a_cell_before_and_during_a_cycle() -> None:
    p0 = rp(RevalStrategy.FIXED, cell_token_budget=1_000)
    case, ts, trials, _ = drive(open_case(), p0, Script(S), 3)
    assert case.state == RevalState.BUDGET_EXHAUSTED and trials == []
    assert "LIFETIME_BUDGET" in triggers(ts)
    p1 = rp(RevalStrategy.FIXED, cell_token_budget=1_200_000, cost_guard=2.0)
    case, ts, trials, spent = drive(open_case(), p1, Script(S, tokens=10_000), 12)
    assert case.state == RevalState.BUDGET_EXHAUSTED and spent <= 1_200_000 + 10_000
    assert "LIFETIME_BUDGET" in triggers(ts)


def test_cases_and_cycles_expire() -> None:
    case, ts, _, _ = drive(open_case(), rp(case_ttl_hours=50.0, dwell=10_000), Script(S), 12)
    assert case.state == RevalState.EXPIRED and triggers(ts)[-1] == "TTL_EXPIRED"
    slow = rp(RevalStrategy.FIXED, max_cycle_hours=5.0, trials_per_round=10)
    case, ts, _, _ = drive(open_case(), slow, Script(S), 6)
    assert case.state == RevalState.EXPIRED and triggers(ts)[-1] == "CYCLE_EXPIRED"
    again, more, _, _ = drive(case, slow, Script(S), 3)
    assert more == []  # expiry is terminal for the episode


def test_cost_futility_stops_a_cycle_early_and_the_guard_vetoes_a_late_pass() -> None:
    p = rp(RevalStrategy.FIXED)
    case, ts, trials, _ = drive(open_case(), p, Script(S, tokens=12_000), 4)  # 2x the reference
    first_fail = next(t for t in ts if t.trigger == RevalTrigger.CRITERIA_NOT_MET)
    assert first_fail.detail == ["cost futility"] and first_fail.n_trials < 30
    assert first_fail.tokens_cycle < 30 * 12_000  # not the full sample
    p2 = rp(RevalStrategy.FIXED, cost_futility_min_trials=10_000 // 1000)
    cheap, ts2, _, _ = drive(open_case(), p2, Script(S, tokens=6500), 12)  # +8 %: within the guard
    assert cheap.state == RevalState.RECOVERED
    never_early, ts3, _, _ = drive(
        open_case(), rp(RevalStrategy.FIXED, cost_guard=1.0), Script(S, tokens=6500), 6
    )
    assert "cost" in " ".join(d for t in ts3 for d in t.detail)  # vetoed at the end


# ---- shadow mode --------------------------------------------------------------------------------
def test_without_execution_readiness_is_recorded_but_nothing_is_spent() -> None:
    src = Script(S)
    case, ts, trials, spent = drive(open_case(), rp(), src, 5, execute=False)
    assert triggers(ts) == ["READY"] and not ts[0].applied
    assert trials == [] and spent == 0 and src.calls == 0
    assert case.state == RevalState.REVALIDATION_PENDING


# ---- fleet composed with the unchanged monitor --------------------------------------------------
def degrade(fleet: RevalidationFleet | MonitorFleet, start: int, n: int, p: float = 0.05,
            family: str = "timeout", seed: int = 1) -> int:  # fmt: skip
    rng = random.Random(seed)
    fleet.ingest([rec(start + i, family, ALT, rng.random() < p) for i in range(n)])
    return start + n


def defaults(fleet: RevalidationFleet | MonitorFleet, start: int, n: int) -> int:
    fleet.ingest([rec(start + i, "timeout", DEF, i % 2 == 0) for i in range(n)])
    return start + n


def new_fleet(strategy: RevalStrategy = RevalStrategy.FIXED, mode: DriftMode = DriftMode.ENFORCE,
              tenant: str | None = "bench", **kw: Any) -> RevalidationFleet:  # fmt: skip
    return RevalidationFleet(
        [ref(0.5)], P.model_copy(update={"mode": mode}), rp(strategy, **kw), tenant
    )


def test_a_quarantine_opens_a_case_with_its_reason_and_disables_native_recovery() -> None:
    fleet = new_fleet(dwell=20)
    i = degrade(fleet, 0, 150)
    case = fleet.cases[CELL]
    assert case.reason == QuarantineReason.PERFORMANCE_DEGRADATION and case.severity > 0.25
    assert fleet.monitor.states[CELL].state == DriftState.QUARANTINED
    assert fleet.monitor.states[CELL].locked  # native recovery off: the existing flag
    assert (
        fleet.transitions[0].trigger == RevalTrigger.CASE_OPENED
        and fleet.transitions[0].from_state is None
    )
    defaults(fleet, i, 300)  # far past M6.2's min_quarantine
    assert fleet.monitor.states[CELL].state == DriftState.QUARANTINED  # a timer does not recover
    plain = MonitorFleet([ref(0.5)], P.model_copy(update={"mode": DriftMode.ENFORCE}), "bench")
    j = degrade(plain, 0, 150)
    defaults(plain, j, 300)
    assert plain.states[CELL].state == DriftState.RECOVERING  # unchanged M6.2 behaviour


def test_restoration_resets_the_monitor_and_a_relapse_counts_toward_the_episode_limit() -> None:
    fleet = new_fleet(RevalidationFleet and RevalStrategy.FIXED, dwell=10)
    i = degrade(fleet, 0, 150)
    i = defaults(fleet, i, 25)  # severe: dwell 2 x 10
    seq_before = fleet.monitor.states[CELL].seq
    script = Script(S)
    for k in range(12):
        fleet.step(script, f"d-{i - 1:05d}", T0 + hours(i + 10 * (k + 1)))
        if fleet.cases[CELL].state != RevalState.RECOVERED:
            assert CELL in fleet.overlay().cells  # pending / revalidating / candidate: disabled
    assert fleet.cases[CELL].state == RevalState.RECOVERED
    st = fleet.monitor.states[CELL]
    assert st.state == DriftState.NORMAL and not st.locked and st.window == ()
    assert st.seq == seq_before and st.episode == 1
    assert fleet.overlay().cells == {}  # trust restored, the controller sees no overlay
    i = degrade(fleet, i + 1000, 150, seed=2)  # relapse
    assert fleet.monitor.states[CELL].state == DriftState.QUARANTINED
    assert fleet.monitor.states[CELL].episode == 2 and fleet.cases[CELL].episode == 2
    assert fleet.cases[CELL].state == RevalState.QUARANTINED  # a fresh case, nothing carried over
    assert fleet.monitor.states[CELL].seq > seq_before  # sequence numbers continue


def test_a_cell_stays_prior_disabled_in_every_case_state_but_recovered() -> None:
    fleet = new_fleet(RevalStrategy.FIXED, dwell=0, confirm_gap_hours=40.0)
    i = degrade(fleet, 0, 150)
    seen: set[RevalState] = set()
    script = Script(S)
    for k in range(30):
        fleet.step(script, f"d-{i - 1:05d}", T0 + hours(i + 10 * k))
        case = fleet.cases[CELL]
        seen.add(case.state)
        assert (CELL in fleet.overlay().cells) == (case.state != RevalState.RECOVERED)
        cell = fleet.overlay().cells.get(CELL)
        assert cell is None or cell.probes_remaining == 0  # no live probes: no user exposure
    assert {RevalState.REVALIDATING, RevalState.RECOVERY_CANDIDATE, RevalState.RECOVERED} <= seen


def test_another_tenants_observations_and_executor_never_enter_a_case() -> None:
    fleet = new_fleet(dwell=0)
    other = [rec(i, "timeout", ALT, False, tenant="other") for i in range(200)]
    fleet.ingest(other)
    assert fleet.cases == {} and fleet.monitor.states[CELL].n_alt == 0
    degrade(fleet, 1000, 150)
    before = fleet.cases[CELL].dwell
    fleet.ingest([rec(2000 + i, "timeout", DEF, True, tenant="other") for i in range(60)])
    assert fleet.cases[CELL].dwell == before  # a foreign tenant's decisions are not our dwell
    foreign = Script(S, tenant="other")
    fleet.step(foreign, "d-01149", T0 + hours(1200))
    assert foreign.calls == 0 and fleet.spent.get(CELL, 0) == 0
    assert fleet.cases[CELL].state == RevalState.QUARANTINED and fleet.cases[CELL].inert
    blocked = [t for t in fleet.transitions if t.trigger == RevalTrigger.INELIGIBLE]
    assert blocked and any(r.startswith("tenant") for t in blocked for r in t.detail)


def test_shadow_mode_never_executes_and_never_restores() -> None:
    fleet = new_fleet(mode=DriftMode.SHADOW, dwell=0)
    i = degrade(fleet, 0, 150)
    src = Script(S)
    for k in range(10):
        fleet.step(src, f"d-{i - 1:05d}", T0 + hours(i + 10 * k))
    assert src.calls == 0 and sum(fleet.spent.values()) == 0 and fleet.trials == []
    assert all(not t.applied for t in fleet.transitions)
    assert fleet.cases[CELL].state == RevalState.REVALIDATION_PENDING


# ---- replay -------------------------------------------------------------------------------------
def run_live(strategy: RevalStrategy) -> tuple[RevalidationFleet, list[Any]]:
    fleet = new_fleet(strategy, dwell=10)
    records = [rec(i, "timeout", ALT, random.Random(9 + i).random() < 0.05) for i in range(150)]
    records += [rec(150 + i, "timeout", DEF, i % 2 == 0) for i in range(40)]
    script = Script(lambda n: S if n % 11 else F)
    done = 0
    for upto in range(0, len(records) + 1, 10):
        fleet.ingest(records[done:upto])
        done = upto
        anchor = records[upto - 1].decision_id if upto else ""
        fleet.step(script, anchor, T0 + hours(upto + 1))
    for k in range(20):  # more rounds with no new decisions: anchors repeat, times do not
        fleet.step(script, records[-1].decision_id, T0 + hours(300 + 10 * k))
    return fleet, records


@pytest.mark.parametrize("strategy", list(RevalStrategy))
def test_replay_from_persisted_evidence_reproduces_every_transition(
    strategy: RevalStrategy,
) -> None:
    live, records = run_live(strategy)
    assert live.trials and any(t.to_state == RevalState.RECOVERED for t in live.transitions)
    drift = P.model_copy(update={"mode": DriftMode.ENFORCE})
    again = replay_revalidation([ref(0.5)], drift, live.params, "bench", records,
                                live.transitions, live.trials)  # fmt: skip
    assert [t.signature() for t in again.transitions] == [t.signature() for t in live.transitions]
    assert [t.model_dump() for t in again.transitions] == [t.model_dump() for t in live.transitions]
    assert [t.key.ident() for t in again.trials] == [t.key.ident() for t in live.trials]
    assert again.overlay().overlay_hash() == live.overlay().overlay_hash()
    assert again.monitor.states == live.monitor.states
    assert [t.signature() for t in again.monitor.transitions] == [
        t.signature() for t in live.monitor.transitions
    ]


def test_replay_stops_exactly_where_the_stored_evidence_stops() -> None:
    live, records = run_live(RevalStrategy.FIXED)
    half = live.trials[: len(live.trials) // 2]
    drift = P.model_copy(update={"mode": DriftMode.ENFORCE})
    part = replay_revalidation([ref(0.5)], drift, live.params, "bench", records,
                               live.transitions, half)  # fmt: skip
    assert len(part.trials) <= len(half)  # the replay never produces evidence of its own
    assert RevalState.RECOVERED not in {t.to_state for t in part.transitions}
    assert RevalTrigger.CONFIRMED not in {t.trigger for t in part.transitions}


def test_a_replay_source_never_invents_a_trial() -> None:
    live, _ = run_live(RevalStrategy.SEQUENTIAL)
    src = ReplaySource(live.trials, live.transitions, "bench")
    stored = live.trials[0]
    assert src.run(ref(0.5), stored.key, stored.result.observed_at) == stored.result
    missing = stored.key.model_copy(update={"ordinal": 99_999})
    assert src.run(ref(0.5), missing, stored.result.observed_at) is None
    assert src.facts(ref(0.5), T0 - hours(1)).tenant_id == "bench"  # before any record: default


def test_a_structural_restoration_rebases_the_monitor_so_the_new_version_is_not_an_alarm() -> None:
    fleet = new_fleet(RevalStrategy.FIXED, dwell=10)
    rng = random.Random(4)
    rt2 = [
        rec(i, "timeout", ALT, rng.random() < 0.5).model_copy(update={"runtime_version": "rt-2"})
        for i in range(200)
    ]
    fleet.ingest(rt2)  # the capability works, but under another runtime than its evidence
    case = fleet.cases[CELL]
    assert case.reason == QuarantineReason.STRUCTURAL_INVALIDATION
    i = defaults(fleet, 200, 30)
    script = Script(S, runtime="rt-2")
    for k in range(14):
        fleet.step(script, f"d-{i - 1:05d}", T0 + hours(i + 10 * (k + 1)))
    assert fleet.cases[CELL].state == RevalState.RECOVERED
    assert fleet.monitor.refs[CELL].runtime_version == "rt-2"  # the monitor's reference moved
    more = [
        rec(i + j, "timeout", ALT, rng.random() < 0.5).model_copy(
            update={"runtime_version": "rt-2"}
        )
        for j in range(120)
    ]
    fleet.ingest(more)
    st = fleet.monitor.states[CELL]
    assert st.state in (DriftState.NORMAL, DriftState.SUSPECT) and st.episode == 1
    assert all(w[2] for w in st.window)  # every observation is compatible with the new reference
