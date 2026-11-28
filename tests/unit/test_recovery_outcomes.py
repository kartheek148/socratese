"""M6.4 Phase 1 - verification-outage semantics: outcome classes, trial accounting versus
evidence accounting, the outage breaker (threshold, probes, resume, maximum suspension, evidence
window, paused clocks), duplicate handling. Every parameter is tested at its boundary."""

from datetime import datetime
from typing import Any

import pytest
from test_drift_monitor import ref
from test_revalidation_core import START, T0, F, S, U, hours, open_case, rp, triggers

from experience_engine.controller.revalidation import CaseState, plan_fixed, step_case
from experience_engine.domain.recovery import RecoveryPolicy, TimeoutOrigin
from experience_engine.domain.revalidation import (
    RejectReason,
    RevalState,
    RevalStrategy,
    RevalTransition,
    RevalTrial,
    RevalTrigger,
    TrialResult,
    TrialVerdict,
)
from recovery_support import POLICY, AttestedScript

I = TrialVerdict.INFRASTRUCTURE_FAILURE  # noqa: E741
X = TrialVerdict.EXECUTION_INCOMPLETE
TO = TrialVerdict.TIMEOUT


def pg(policy: RecoveryPolicy = POLICY, strategy: RevalStrategy = RevalStrategy.FIXED, **kw: Any):  # type: ignore[no-untyped-def]
    return rp(strategy, recovery=policy, **kw)


def policy(**kw: Any) -> RecoveryPolicy:
    return POLICY.model_copy(update=kw)


class Bench:
    """One case driven round by round (10 h apart) against a world that can degrade: `attempt`
    forces the verdict of every attempt, `down` / `infra_down` are what the executor reports."""

    def __init__(self, params: Any = None, *, case: CaseState | None = None, **src: Any) -> None:
        self.params = params or pg()
        self.attempt: TrialVerdict | None = None
        self.down = False
        self.infra_down = False
        self.src = AttestedScript(
            lambda _n: self.attempt or S,
            down=lambda _now: self.down,
            infra_down=lambda _now: self.infra_down,
            **src,
        )
        self.case = case or open_case()
        self.k = 0
        self.transitions: list[RevalTransition] = []
        self.trials: list[RevalTrial] = []
        self.spent = 0
        self.ref = ref(0.5)

    def round(self, at: datetime | None = None) -> list[RevalTransition]:
        now = at or START + hours(10 * self.k)
        self.k += 1
        res = step_case(
            self.ref, self.params, self.case, execute=True, tenant_id="bench", source=self.src,
            facts=self.src.facts(self.ref, now), lifetime_spent=self.spent, anchor=f"a{self.k}",
            now=now, applied=True,
        )  # fmt: skip
        self.case, self.spent = res.case, self.spent + res.tokens
        self.transitions += res.transitions
        self.trials += res.trials
        return res.transitions

    def rounds(self, n: int) -> list[RevalTransition]:
        return [t for _ in range(n) for t in self.round()]

    def suspend_by_attempts(self) -> None:
        """One healthy evidence round, then attempts that keep failing until the breaker opens."""
        self.round()
        self.attempt = U
        self.round()
        self.attempt = None
        assert self.case.suspended


def result(verdict: TrialVerdict, **kw: Any) -> TrialResult:
    return TrialResult(verdict=verdict, tokens=1, task_ref="t", observed_at=T0, **kw)


# ---- the seven outcome classes ------------------------------------------------------------------
def test_verified_success_and_failure_are_the_evidence_and_nothing_else_is() -> None:
    case = Bench(pg())
    case.src.verdict = lambda n: S if n % 2 else F
    case.round()
    assert (case.case.n, case.case.k) == (50, 25)  # success evidence and failure evidence
    assert case.case.n_unverified == case.case.n_infra == case.case.n_incomplete == 0


def test_verification_unavailable_moves_outage_accounting_only() -> None:
    b = Bench(pg())
    b.src.verdict = lambda n: U if n in (3, 9) else S
    b.round()
    c = b.case
    assert (c.n, c.k) == (48, 48)  # no capability evidence either way
    assert c.n_unverified == 2 and c.tokens_unverified == 12_000
    assert c.tokens_cycle == 50 * 6000 and c.tokens_counted == 48 * 6000  # spend is still counted
    assert c.cycle == 0 and c.outage_streak == 0 and not c.suspended


def test_an_infrastructure_failure_is_not_a_capability_failure() -> None:
    b = Bench(pg())
    b.src.verdict = lambda n: I if n in (4, 5) else S
    b.round()
    c = b.case
    assert (c.n, c.k) == (48, 48)
    assert (c.n_infra, c.tokens_infra, c.n_unverified) == (2, 12_000, 0)


def test_an_incomplete_trial_adds_no_evidence_but_its_tokens_are_accounted() -> None:
    b = Bench(pg(), tokens=lambda n: 4_000 if n == 7 else 6_000)
    b.src.verdict = lambda n: X if n == 7 else S
    b.round()
    c = b.case
    assert (c.n, c.k, c.n_incomplete, c.tokens_incomplete) == (49, 49, 1, 4_000)
    assert b.spent == 4_000 + 49 * 6_000 == c.tokens_cycle  # actual resources, every attempt
    assert c.tokens_counted == 49 * 6_000  # but only evidence tokens feed the cost test


def test_a_capability_timeout_is_failure_evidence_and_an_infrastructure_timeout_is_not() -> None:
    b = Bench(pg(), origin={3: TimeoutOrigin.CAPABILITY, 8: TimeoutOrigin.INFRASTRUCTURE})
    b.src.verdict = lambda n: TO if n == 3 else (I if n == 8 else S)
    b.round()
    assert (b.case.n, b.case.k) == (49, 48)  # the capability timeout counts as a failure
    assert (b.case.n_infra, b.case.n_unverified) == (1, 0)  # the infrastructure one does not
    stored = {t.key.ordinal: t.result for t in b.trials}
    assert stored[3].timeout_origin == TimeoutOrigin.CAPABILITY and stored[3].counted
    assert stored[8].timeout_origin == TimeoutOrigin.INFRASTRUCTURE and not stored[8].counted
    assert stored[3].outcome_class.value == "CAPABILITY_TIMEOUT"
    assert stored[8].outcome_class.value == "INFRASTRUCTURE_FAILURE"


def test_an_unattributed_timeout_is_counted_against_the_capability_never_hidden() -> None:
    b = Bench(pg())
    b.src.verdict = lambda n: TO if n % 2 else S  # the adapter did not say who timed out
    b.round()
    assert (b.case.n, b.case.k) == (50, 25)  # conservative: a failure, never silently dropped


def test_timeout_origin_must_match_the_outcome() -> None:
    result(TO, timeout_origin=TimeoutOrigin.CAPABILITY)
    result(I, timeout_origin=TimeoutOrigin.INFRASTRUCTURE)
    for verdict, origin in (
        (TO, TimeoutOrigin.INFRASTRUCTURE),  # an infrastructure timeout is not a capability one
        (I, TimeoutOrigin.CAPABILITY),
        (S, TimeoutOrigin.CAPABILITY),
        (U, TimeoutOrigin.INFRASTRUCTURE),
    ):
        with pytest.raises(ValueError, match="timeout origin"):
            result(verdict, timeout_origin=origin)


# ---- trial accounting is separate from evidence accounting --------------------------------------
def test_attempt_budget_and_evidence_counters_are_tracked_independently() -> None:
    b = Bench(pg())
    b.src.verdict = lambda n: U if n % 5 == 0 else S
    b.rounds(2)
    c = b.case
    assert c.ordinal == 100  # attempts: the budget the caps watch
    assert c.n == 80 and c.n_unverified == 20  # evidence: what the statistics see
    assert b.spent == c.tokens_cycle == 100 * 6000
    b.rounds(3)  # the whole fixed sample is evidence, however many attempts it took
    met = next(t for t in b.transitions if t.trigger == RevalTrigger.CRITERIA_MET)
    m = met.measurements
    assert m["n"] == m["plan_n"] and m["attempts_evidence_stage"] > m["n"]
    assert m["unverified"] == m["attempts_evidence_stage"] - m["n"]
    assert m["tokens_unverified"] == 6000 * m["unverified"] and m["outage_streak"] == 0


def test_intermittent_unavailability_is_bounded_by_the_attempt_budget_and_never_costs_a_cycle() -> (
    None
):
    plan = plan_fixed(0.3, 0.5, 0.05, 0.20, 320)
    assert plan is not None and plan.n > 33  # a third of 100 attempts cannot satisfy it
    b = Bench(pg(margin=0.2, max_trials=100))
    b.src.verdict = lambda n: S if n % 3 == 2 else U  # never three in a row
    b.rounds(4)
    assert b.case.state == RevalState.BUDGET_EXHAUSTED
    assert triggers(b.transitions)[-1] == "TRIAL_CAP"
    assert b.case.ordinal == 100 and b.case.n == 33 and b.case.cycle == 0
    assert "SUSPENDED" not in triggers(b.transitions)
    assert "VERIFICATION_UNAVAILABLE" not in triggers(b.transitions)


# ---- the breaker ---------------------------------------------------------------------------------
@pytest.mark.parametrize("threshold", [1, 2, 3, 5])
def test_the_breaker_opens_after_exactly_the_configured_number_of_unavailable_attempts(
    threshold: int,
) -> None:
    b = Bench(pg(policy(outage_threshold=threshold)))
    b.src.verdict = lambda n: U if n < threshold else S
    b.round()
    assert b.case.suspended and b.case.ordinal == threshold == b.src.calls
    assert triggers(b.transitions) == ["READY", "TRIALS_STARTED", "SUSPENDED"]
    assert b.case.cycle == 0 and b.case.state == RevalState.REVALIDATING


@pytest.mark.parametrize("threshold", [2, 3, 5])
def test_one_attempt_short_of_the_threshold_does_not_open_the_breaker(threshold: int) -> None:
    b = Bench(pg(policy(outage_threshold=threshold)))
    b.src.verdict = lambda n: U if n < threshold - 1 else S
    b.round()
    assert not b.case.suspended and "SUSPENDED" not in triggers(b.transitions)
    assert b.case.n == 50 - (threshold - 1)


def test_unavailable_and_infrastructure_attempts_share_one_streak_and_evidence_resets_it() -> None:
    mixed = Bench()
    mixed.src.verdict = lambda n: (U, I, U)[n] if n < 3 else S
    mixed.round()
    assert mixed.case.suspended and mixed.case.ordinal == 3
    reset = Bench()
    reset.src.verdict = lambda n: S if n == 2 else (U if n < 5 else S)  # U U S U U
    reset.round()
    assert not reset.case.suspended and reset.case.outage_streak == 0


def test_an_interrupted_attempt_neither_extends_nor_resets_the_streak() -> None:
    b = Bench()
    b.src.verdict = lambda n: (U, U, X, U)[n] if n < 4 else S  # U U X U: the X is neutral
    b.round()
    assert b.case.suspended and b.case.ordinal == 4 and b.case.n_incomplete == 1


def test_the_breaker_keeps_the_evidence_and_stops_spending() -> None:
    b = Bench()
    b.round()
    assert (b.case.n, b.case.k) == (50, 50)
    b.attempt = U
    new = b.round()
    assert triggers(new) == ["SUSPENDED"] and b.case.cycle == 0
    assert (b.case.n, b.case.k) == (50, 50)  # accumulated before the outage: kept
    assert b.case.tokens_unverified == 3 * 6000  # the three wasted attempts are accounted for
    calls, spent = b.src.calls, b.spent
    b.down = True
    for _ in range(4):
        assert b.round() == []  # nothing happens, nothing is written
    assert (b.src.calls, b.spent) == (calls, spent)  # and nothing is spent or executed


def test_probes_alone_suspend_after_the_same_threshold_and_spend_nothing() -> None:
    b = Bench()
    b.round()
    spent = b.spent
    b.down = True
    first, second, third = b.round(), b.round(), b.round()
    assert triggers(first) == triggers(second) == ["OUTAGE_PROBE"]
    assert first[0].measurements["outage_streak"] == 1.0
    assert second[0].measurements["outage_streak"] == 2.0
    assert triggers(third) == ["SUSPENDED"] and b.case.suspended
    assert b.spent == spent and b.case.cycle == 0


def test_a_healthy_probe_clears_the_streak_and_the_clearing_is_recorded() -> None:
    b = Bench()
    b.round()
    b.down = True
    b.round()
    assert b.case.outage_streak == 1
    b.down = False
    new = b.round()
    assert triggers(new)[0] == "OUTAGE_PROBE" and new[0].measurements["healthy"] == 1.0
    assert b.case.outage_streak == 0 and not b.case.suspended
    assert b.case.n > 50  # the same round went on collecting evidence


def test_infrastructure_unavailability_suspends_like_verification_unavailability() -> None:
    b = Bench()
    b.round()
    b.infra_down = True
    b.rounds(3)
    assert b.case.suspended


# ---- resume --------------------------------------------------------------------------------------
@pytest.mark.parametrize("probes", [1, 2, 3])
def test_resume_needs_exactly_the_configured_consecutive_healthy_probes(probes: int) -> None:
    b = Bench(pg(policy(resume_probes=probes)))
    b.suspend_by_attempts()
    evidence = (b.case.n, b.case.k)
    calls = b.src.calls
    for _ in range(1, probes):
        new = b.round()
        assert triggers(new) == ["OUTAGE_PROBE"] and b.case.suspended
        assert (b.case.n, b.case.k, b.src.calls) == (*evidence, calls)  # probes are not evidence
    new = b.round()
    assert triggers(new)[:1] == ["RESUMED"] and not b.case.suspended
    assert b.case.n > evidence[0]  # collection continues once the path is operational


def test_probes_are_not_trials_and_never_count_as_positive_evidence() -> None:
    b = Bench()
    b.suspend_by_attempts()
    n, calls, trials = b.case.n, b.src.calls, len(b.trials)
    new = b.round()  # the first healthy probe
    assert triggers(new) == ["OUTAGE_PROBE"]
    assert (b.case.n, b.src.calls, len(b.trials)) == (n, calls, trials)


def test_an_unhealthy_probe_restarts_the_resume_count() -> None:
    b = Bench()
    b.suspend_by_attempts()
    b.round()  # healthy 1/2
    assert b.case.healthy_probes == 1
    b.down = True
    new = b.round()  # unhealthy: the hysteresis resets the count
    assert triggers(new) == ["OUTAGE_PROBE"] and b.case.healthy_probes == 0
    b.down = False
    b.round()  # healthy 1/2 again: not resumed yet
    assert b.case.suspended
    assert triggers(b.round())[:1] == ["RESUMED"]


def test_resuming_resets_the_outage_counters_deterministically() -> None:
    b = Bench()
    b.suspend_by_attempts()
    b.round()
    b.round()
    c = b.case
    assert (c.outage_streak, c.healthy_probes, c.suspended, c.suspended_at) == (0, 0, False, None)
    assert c.suspended_hours > 0


def test_an_outage_that_returns_before_resume_restarts_cleanly() -> None:
    b = Bench()
    b.suspend_by_attempts()
    b.round()
    b.round()  # resumed and collecting
    b.attempt = U
    b.round()  # a second outage: three more failed attempts open the breaker again
    assert b.case.suspended and b.case.outage_streak == 3
    assert b.case.cycle == 0 and b.case.suspended_hours > 0


# ---- maximum suspension, paused clocks, evidence window ------------------------------------------
@pytest.mark.parametrize(("cap", "expires"), [(60.0, False), (59.99, True)])
def test_the_maximum_suspension_is_inclusive_at_the_boundary(cap: float, expires: bool) -> None:
    b = Bench(pg(policy(max_suspension_hours=cap)))
    b.suspend_by_attempts()
    at = b.case.suspended_at
    assert at is not None
    b.down = True
    new = b.round(at=at + hours(60))
    assert (b.case.state == RevalState.EXPIRED) is expires
    if expires:
        assert triggers(new) == ["SUSPENSION_TIMEOUT"]
        assert new[0].measurements["suspended_total_hours"] == 60.0
    else:
        assert b.case.suspended


def test_an_expired_suspension_never_recovers_and_is_terminal() -> None:
    b = Bench(pg(policy(max_suspension_hours=20.0)))
    b.suspend_by_attempts()
    at = b.case.suspended_at
    assert at is not None
    b.down = True
    b.round(at=at + hours(30))
    assert b.case.state == RevalState.EXPIRED
    calls = b.src.calls
    b.down = False
    for _ in range(3):
        assert b.round() == []  # terminal: nothing more happens, in particular no recovery
    assert b.src.calls == calls


def test_the_suspension_cap_is_cumulative_over_spells() -> None:
    b = Bench(pg(policy(max_suspension_hours=70.0)))
    b.suspend_by_attempts()
    at = b.case.suspended_at
    assert at is not None
    b.round(at=at + hours(40))  # healthy probe 1
    b.round(at=at + hours(50))  # resumed: 50 h used
    assert b.case.suspended_hours == 50.0
    b.attempt = U
    b.round(at=at + hours(60))  # suspended again
    assert b.case.suspended
    b.down = True
    b.round(at=at + hours(60 + 20))  # 50 + 20 = 70: still within
    assert b.case.suspended
    b.round(at=at + hours(60 + 20 + 1))  # 71 h cumulative
    assert b.case.state == RevalState.EXPIRED


def test_case_and_cycle_clocks_do_not_run_while_suspended() -> None:
    p = pg(case_ttl_hours=200.0, max_cycle_hours=150.0)
    b = Bench(p)
    b.round()
    b.attempt = U
    b.round()  # suspended
    b.attempt = None
    b.down = True
    at = b.case.suspended_at
    assert at is not None
    b.round(at=at + hours(300))  # far longer than the TTL and the cycle limit: no expiry
    assert b.case.suspended and b.case.state == RevalState.REVALIDATING
    b.down = False
    b.round(at=at + hours(310))
    b.round(at=at + hours(320))  # resumed after 320 h
    assert not b.case.suspended
    assert b.case.opened_at == T0 + hours(320)  # the anchors moved by the paused time
    for i in range(1, 13):
        b.round(at=at + hours(320 + 10 * i))
    assert b.case.state == RevalState.RECOVERED
    assert not {"TTL_EXPIRED", "CYCLE_EXPIRED"} & set(triggers(b.transitions))


def test_evidence_inside_the_window_survives_an_outage() -> None:
    b = Bench()
    b.suspend_by_attempts()
    at = b.case.suspended_at
    assert at is not None
    n = b.case.n
    b.round(at=at + hours(30))
    new = b.round(at=at + hours(40))
    assert "EVIDENCE_EXPIRED" not in triggers(new) and b.case.n >= n  # kept and extended


@pytest.mark.parametrize(("age", "expired"), [(110.0, False), (110.1, True)])
def test_evidence_expires_at_the_window_measured_at_the_end_of_the_round(
    age: float, expired: bool
) -> None:
    b = Bench()
    b.round()  # evidence since START
    new = b.round(at=START + hours(age))  # round end = age + 10 h: 120 h is still inside
    assert ("EVIDENCE_EXPIRED" in triggers(new)) is expired


def test_expired_evidence_is_discarded_without_consuming_a_cycle_or_reusing_a_trial_key() -> None:
    b = Bench()
    b.round()
    assert (b.case.n, b.case.cycle) == (50, 0)
    new = b.round(at=START + hours(200))
    assert triggers(new)[0] == "EVIDENCE_EXPIRED"
    c = b.case
    assert c.cycle == 0 and c.expired_evidence == 1 and c.state == RevalState.REVALIDATING
    assert c.n == 50 and c.k == 50  # fresh evidence collected after the reset, in the same round
    keys = [t.key.ident() for t in b.trials]
    assert len(keys) == len(set(keys)) and c.evidence_since is not None
    assert c.evidence_since > START + hours(190)
    assert c.tokens_cycle == 100 * 6000  # the budget spent on the discarded evidence stays spent


def test_a_resume_after_the_window_discards_the_old_evidence_and_not_the_cycle() -> None:
    b = Bench()
    b.suspend_by_attempts()
    at = b.case.suspended_at
    assert at is not None
    b.round(at=at + hours(200))
    new = b.round(at=at + hours(210))  # resumed, but the evidence is 200+ h old
    assert triggers(new)[:2] == ["RESUMED", "EVIDENCE_EXPIRED"]
    assert b.case.cycle == 0 and b.case.expired_evidence == 1


# ---- duplicates ----------------------------------------------------------------------------------
def test_a_task_delivered_twice_counts_once() -> None:
    b = Bench(pg(), over={1: {"task_ref": "t0"}})
    b.round()
    assert b.case.n == 49 and b.case.n_rejected == 1
    dup = [t for t in b.trials if not t.accepted]
    assert [t.reject_reason for t in dup] == [RejectReason.DUPLICATE]
    assert b.case.tokens_rejected == 6000  # still accounted for


def test_an_execution_delivered_twice_counts_once() -> None:
    b = Bench(pg(), prov={1: {"execution_id": "x0"}})
    b.round()
    assert b.case.n == 49
    assert [t.reject_reason for t in b.trials if not t.accepted] == [RejectReason.DUPLICATE]


def test_a_task_whose_attempt_had_no_verdict_may_be_attempted_again() -> None:
    b = Bench(pg(), over={1: {"task_ref": "t0"}})
    b.src.verdict = lambda n: U if n == 0 else S
    b.round()
    assert b.case.n == 49 and b.case.n_rejected == 0  # the retry of t0 is evidence, not a duplicate


# ---- the M6.3 semantics are what they were -------------------------------------------------------
def test_without_a_recovery_block_an_outage_still_fails_the_cycle_exactly_as_in_m6_3() -> None:
    script = lambda n: U if n < 15 else S  # noqa: E731
    legacy = Bench(rp(RevalStrategy.FIXED))
    legacy.src.verdict = script
    legacy.round()
    assert "VERIFICATION_UNAVAILABLE" in triggers(legacy.transitions)
    assert legacy.case.cycle == 1  # an outage consumed a cycle: the flaw M6.4 removes
    gated = Bench(pg())
    gated.src.verdict = script
    gated.round()
    assert "VERIFICATION_UNAVAILABLE" not in triggers(gated.transitions)
    assert gated.case.cycle == 0 and gated.case.suspended


# ---- the confirmation stage obeys the same semantics ---------------------------------------------
PLAN_N = 158  # the frozen FIXED plan for the reference used here (p = 0.5, margin 0.10)


def confirmation_bench(verdict: Any, **kw: Any) -> Bench:
    b = Bench(pg(confirm_gap_hours=0.0, **kw))
    b.src.verdict = verdict
    return b


def test_an_outage_at_confirmation_suspends_the_candidate_and_keeps_its_evidence() -> None:
    b = confirmation_bench(lambda n: U if PLAN_N <= n < PLAN_N + 3 else S)
    b.rounds(5)  # four rounds of evidence; the candidate's first confirmation attempts fail
    c = b.case
    assert c.state == RevalState.RECOVERY_CANDIDATE and c.suspended and c.cycle == 0
    assert (c.n, c.k, c.c_n, c.c_ordinal) == (PLAN_N, PLAN_N, 0, 3)  # exactly three attempts
    assert c.tokens_unverified == 3 * 6000 and c.c_unverified == 3
    b.round()  # a healthy probe: 1/2
    assert b.case.suspended
    b.round()  # resumed, and the confirmation completes in the same round
    assert triggers(b.transitions)[-2:] == ["RESUMED", "CONFIRMED"]
    assert b.case.state == RevalState.RECOVERED and b.case.cycle == 0
    assert b.transitions[-1].gates["eligible"] is True


def test_unavailable_confirmation_attempts_are_not_confirmation_evidence() -> None:
    b = confirmation_bench(
        lambda n: U if n in (PLAN_N, PLAN_N + 1) else S
    )  # two: below the breaker
    b.rounds(5)
    assert b.case.state == RevalState.RECOVERED
    assert (b.case.c_n, b.case.c_ordinal, b.case.c_unverified) == (20, 22, 2)
    gates = b.transitions[-1].gates
    assert sum(1 for i in gates["evidence_ids"] if "|C" in i) == 20  # twenty, not eighteen


def test_confirmation_attempts_are_bounded_by_the_attempt_budget() -> None:
    plan = plan_fixed(0.3, 0.5, 0.05, 0.20, 320)
    assert plan is not None
    n = plan.n
    b = confirmation_bench(
        lambda i: S if i < n or (i - n) % 3 == 2 else U,  # confirmation: one in three has a verdict
        margin=0.2, max_trials=100, confirm_trials=40,
    )  # fmt: skip
    b.rounds(8)
    assert b.case.state == RevalState.BUDGET_EXHAUSTED
    assert triggers(b.transitions)[-1] == "TRIAL_CAP" and b.case.c_ordinal == 100
    assert b.case.cycle == 0 and b.case.c_n == 33  # never recovered on 33 of the 40 required
