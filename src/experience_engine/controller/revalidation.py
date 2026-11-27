"""Controlled revalidation of quarantined cells (M6.3, pure).

A case is opened for each quarantine episode of the (unchanged) M6.2 monitor. The case decides
whether the cell may be tested (eligibility gates), when (strategy readiness), how (bounded,
independently verified, out-of-band trials with exact error control) and whether it may be
restored (criterion, candidate, confirmation). Nothing here executes a model: trials arrive from a
`TrialSource` port, and a replay source returning persisted trials reproduces every transition.
Design: docs/experiments/revalidation-design.md.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from experience_engine.controller.drift import CellReference, MonitorFleet
from experience_engine.domain.controller import DecisionOutcomeRecord, PairedCell
from experience_engine.domain.drift import (
    Detector,
    DriftMode,
    DriftOverlay,
    DriftParams,
    DriftState,
    DriftTransition,
    DriftTrigger,
)
from experience_engine.domain.outcome import OutcomeStatus, VerificationResult
from experience_engine.domain.revalidation import (
    TERMINAL_STATES,
    ExecutorFacts,
    QuarantineReason,
    RejectReason,
    RevalidationParams,
    RevalState,
    RevalStrategy,
    RevalTransition,
    RevalTrial,
    RevalTrigger,
    TrialKey,
    TrialResult,
    TrialStage,
    TrialVerdict,
)
from experience_engine.domain.stats import newcombe_difference_bounds, normal_quantile

_P_MIN, _P_MAX = 0.01, 0.99


# ---- reasons -----------------------------------------------------------------------------------
def reason_of(trigger: DriftTrigger, detector: Detector) -> QuarantineReason:
    """Projection of the existing drift trigger onto the quarantine reasons (design §2.1)."""
    if trigger == DriftTrigger.SUCCESS_DEGRADATION:
        return (
            QuarantineReason.STATISTICAL_DRIFT
            if detector == Detector.CUSUM
            else QuarantineReason.PERFORMANCE_DEGRADATION
        )
    if trigger in (DriftTrigger.COST_DEGRADATION, DriftTrigger.UTILITY_DEGRADATION):
        return QuarantineReason.COST_DEGRADATION
    if trigger == DriftTrigger.CONTEXT_MISMATCH:
        return QuarantineReason.STRUCTURAL_INVALIDATION
    if trigger == DriftTrigger.EVIDENCE_STALE:
        return QuarantineReason.EVIDENCE_STALE
    return QuarantineReason.UNKNOWN


def reason_and_severity(
    quarantine: DriftTransition, preceding: Iterable[DriftTransition], detector: Detector
) -> tuple[QuarantineReason, float]:
    """The reason is the trigger of the DRIFTED transition immediately before the QUARANTINED one
    (same cell, same decision). Severity: the success drop recorded when drift was confirmed."""
    drifted = next(
        (
            t
            for t in preceding
            if t.cell_key == quarantine.cell_key
            and t.seq == quarantine.seq - 1
            and t.to_state == DriftState.DRIFTED
            and t.decision_id == quarantine.decision_id
        ),
        None,
    )
    if drifted is None:
        return QuarantineReason.UNKNOWN, 0.0
    m = drifted.measurements
    drop = (
        m["ref_success"] - m["success_rate"] if "ref_success" in m and "success_rate" in m else 0.0
    )
    return reason_of(drifted.trigger, detector), max(0.0, drop)


# ---- exact statistics --------------------------------------------------------------------------
def _log_pmf(k: int, n: int, p: float) -> float:
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log1p(-p)
    )


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Bin(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return min(1.0, sum(math.exp(_log_pmf(j, n, p)) for j in range(k, n + 1)))


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Bin(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return min(1.0, sum(math.exp(_log_pmf(j, n, p)) for j in range(0, k + 1)))


def critical_k(n: int, p_bad: float, alpha: float) -> int:
    """Smallest k with P(X >= k | p_bad) <= alpha (n + 1 when none): an exact level-alpha test."""
    for k in range(0, n + 2):
        if binom_sf(k, n, p_bad) <= alpha:
            return k
    return n + 1


@dataclass(frozen=True)
class FixedPlan:
    n: int
    k_crit: int
    alpha: float
    size: float  # exact P(recover | p_bad)
    power: float  # exact P(recover | p_good)


def hypotheses(ref: CellReference, margin: float) -> tuple[float, float]:
    p_good = min(max(ref.p_alt, _P_MIN + 0.01), _P_MAX)
    p_bad = max(p_good - margin, _P_MIN)
    return p_bad, p_good


def plan_fixed(
    p_bad: float, p_good: float, alpha: float, beta: float, max_trials: int
) -> FixedPlan | None:
    """Smallest N whose exact one-sided test has size <= alpha and exact power >= 1 - beta at
    p_good (search starts at the normal approximation; discreteness makes power non-monotone, so
    the first N that satisfies both is taken)."""
    if p_good - p_bad < 1e-9:
        return None
    z_a, z_b = normal_quantile(1.0 - alpha), normal_quantile(1.0 - beta)
    n0 = math.ceil(
        ((z_a * math.sqrt(p_bad * (1 - p_bad)) + z_b * math.sqrt(p_good * (1 - p_good)))
         / (p_good - p_bad)) ** 2
    )  # fmt: skip
    for n in range(max(10, n0 - 5), max_trials + 1):
        k = critical_k(n, p_bad, alpha)
        if k > n:
            continue
        power = binom_sf(k, n, p_good)
        if power >= 1.0 - beta:
            return FixedPlan(n, k, alpha, binom_sf(k, n, p_bad), power)
    return None


def sprt_bounds(alpha: float, beta: float) -> tuple[float, float]:
    """(lower, upper) log-likelihood-ratio boundaries; upper accepts p_good (recover)."""
    return math.log(beta / (1.0 - alpha)), math.log((1.0 - beta) / alpha)


def sprt_step(p_bad: float, p_good: float, success: bool) -> float:
    return math.log(p_good / p_bad) if success else math.log((1.0 - p_good) / (1.0 - p_bad))


# ---- verification ------------------------------------------------------------------------------
def classify_run_outcome(
    status: OutcomeStatus, results: Sequence[VerificationResult], *, timed_out: bool = False,
    invalid_output: bool = False,
) -> TrialVerdict:  # fmt: skip
    """Independent-verification mapping. A success needs VERIFIED_SUCCESS *and* every goal
    criterion explicitly passed; anything the verifier could not evaluate is unavailable, never a
    success. The executor adapter, not free text, reports timeouts and invalid outputs."""
    goals = [r for r in results if r.role == "goal"]
    if status == OutcomeStatus.UNKNOWN or any(r.passed is None for r in results):
        return TrialVerdict.VERIFICATION_UNAVAILABLE
    if timed_out:
        return TrialVerdict.TIMEOUT
    if invalid_output:
        return TrialVerdict.INVALID_OUTPUT
    if status == OutcomeStatus.VERIFIED_SUCCESS and goals and all(r.passed is True for r in goals):
        return TrialVerdict.SUCCESS
    return TrialVerdict.FAILURE  # FAILURE and PARTIAL


# ---- ports -------------------------------------------------------------------------------------
class TrialSource(Protocol):
    """Executes (or replays) revalidation trials. `run` returns None when no result is available
    (no capacity; in a replay, nothing persisted for that key)."""

    def facts(self, ref: CellReference, now: datetime) -> ExecutorFacts: ...

    def run(self, ref: CellReference, key: TrialKey, at: datetime) -> TrialResult | None: ...


# ---- eligibility -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...] = ()
    permanent: bool = False
    permanent_trigger: RevalTrigger | None = None


@dataclass(frozen=True)
class CaseState:
    cell_key: str
    episode: int
    reason: QuarantineReason
    severity: float
    trigger_decision_id: str
    trigger_at: datetime | None
    opened_at: datetime | None
    state: RevalState = RevalState.QUARANTINED
    cycle: int = 0  # failed cycles so far
    seq: int = 0
    inert: bool = False  # permanently ineligible (recorded once)
    dwell: int = 0  # default-served verified decisions since opening / the last failed cycle
    default_n: int = 0
    default_k: int = 0
    last_ineligible: tuple[str, ...] = ()
    # cycle evidence
    cycle_started_at: datetime | None = None
    alpha: float = 0.05
    plan_n: int = 0
    plan_k: int = 0
    ordinal: int = 0
    n: int = 0
    k: int = 0
    n_unverified: int = 0
    n_rejected: int = 0
    mismatches: int = 0
    tokens_cycle: int = 0
    tokens_counted: int = 0
    tokens_sq: float = 0.0
    llr: float = 0.0
    versions: tuple[str | None, str | None] | None = None
    task_refs: frozenset[str] = frozenset()
    first_ordinal: int | None = None
    last_trial_at: datetime | None = None
    # candidate / confirmation
    candidate_at: datetime | None = None
    c_ordinal: int = 0
    c_n: int = 0
    c_k: int = 0
    c_unverified: int = 0


def required_dwell(params: RevalidationParams, case: CaseState) -> int:
    return params.dwell * (2 if case.severity >= params.severe_drop else 1)


def case_alpha(params: RevalidationParams, case: CaseState) -> float:
    return params.alpha_severe if case.severity >= params.severe_drop else params.alpha


def planned_trials(params: RevalidationParams, case: CaseState, ref: CellReference) -> int | None:
    """Trials the cycle needs at minimum (fixed N; for the sequential strategy a floor), or None
    when the hypotheses cannot be planned."""
    p_bad, p_good = hypotheses(ref, params.margin)
    plan = plan_fixed(p_bad, p_good, case_alpha(params, case), params.beta, params.max_trials)
    if plan is None:
        return None
    return plan.n if params.strategy != RevalStrategy.SEQUENTIAL else min(plan.n, 30)


def check_eligibility(
    params: RevalidationParams,
    case: CaseState,
    ref: CellReference,
    *,
    tenant_id: str,
    facts: ExecutorFacts | None,
    lifetime_spent: int,
) -> Eligibility:
    """The gate list of design §2.2 (readiness is separate). One reason per failed gate."""
    reasons: list[str] = []
    permanent: RevalTrigger | None = None
    perm_flag = False

    def gate(name: str, ok: bool, why: str, *, perm: bool = False,
             trigger: RevalTrigger | None = None) -> None:  # fmt: skip
        nonlocal permanent, perm_flag
        if not ok:
            reasons.append(f"{name}: {why}")
            if perm:
                perm_flag = True
                permanent = trigger or permanent

    gate("reason_known", case.reason != QuarantineReason.UNKNOWN, "quarantine reason unknown",
         perm=True)  # fmt: skip
    gate("risk", ref.risk in params.allowed_risks, f"risk class {ref.risk!r} not revalidatable",
         perm=True)  # fmt: skip
    gate("cycles", case.cycle < params.max_cycles, f"{case.cycle} failed cycles",
         perm=True, trigger=RevalTrigger.CYCLES_EXHAUSTED)  # fmt: skip
    need = planned_trials(params, case, ref)
    gate("plan", need is not None, "hypotheses cannot be planned within max_trials",
         perm=True)  # fmt: skip
    if need is not None:
        planned_tokens = int(need * ref.mean_tokens)
        gate("budget", lifetime_spent + planned_tokens <= params.cell_token_budget,
             f"{lifetime_spent} spent + {planned_tokens} planned > {params.cell_token_budget}",
             perm=True, trigger=RevalTrigger.LIFETIME_BUDGET)  # fmt: skip
    if facts is not None:
        gate("tenant", facts.tenant_id == tenant_id,
             f"executor tenant {facts.tenant_id!r} != {tenant_id!r}", perm=True)  # fmt: skip
        gate("identity", facts.supported, "executor cannot test this capability")
        known = facts.runtime_version is not None and facts.verifier_version is not None
        gate("versions_known", known, "executor reports no runtime/verifier version")
        gate("verification_available", facts.verification_available,
             "independent verification unavailable")  # fmt: skip
        gate("pool", facts.pool >= params.min_pool, f"{facts.pool} < {params.min_pool} tasks")
    if case.reason == QuarantineReason.VERIFICATION_FAILURE and (
        facts is None or not facts.verification_available
    ):
        gate("reason_revalidatable", False, "quarantined for verification failure")
    return Eligibility(not reasons, tuple(reasons), perm_flag, permanent)


def strategy_ready(
    params: RevalidationParams, case: CaseState, ref: CellReference
) -> tuple[bool, dict[str, float]]:
    """Strategy readiness (design §4). Only A/B/C differ here."""
    need = required_dwell(params, case)
    meas: dict[str, float] = {"dwell": float(case.dwell), "dwell_required": float(need)}
    if case.dwell < need:
        return False, meas
    if params.strategy != RevalStrategy.EVIDENCE_THRESHOLD:
        return True, meas
    meas["default_n"] = float(case.default_n)
    if case.default_n < params.ready_default_min:
        return False, meas
    _, ub = newcombe_difference_bounds(
        case.default_k, case.default_n, ref.k_def, ref.n, params.ready_confidence
    )
    meas["default_success_diff_ub"] = ub
    return ub >= -params.margin, meas  # not confidently degraded


# ---- the case machine --------------------------------------------------------------------------
@dataclass
class StepOutput:
    case: CaseState
    transitions: list[RevalTransition] = field(default_factory=list)
    trials: list[RevalTrial] = field(default_factory=list)
    tokens: int = 0
    restored: RevalTransition | None = None


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def _conflict(ref: CellReference, res: TrialResult) -> bool:
    if ref.runtime_version and res.runtime_version and ref.runtime_version != res.runtime_version:
        return True
    return bool(
        ref.verifier_version
        and res.verifier_version
        and ref.verifier_version != res.verifier_version
    )


def step_case(
    ref: CellReference,
    params: RevalidationParams,
    case: CaseState,
    *,
    execute: bool,
    tenant_id: str,
    source: TrialSource | None,
    facts: ExecutorFacts | None,
    lifetime_spent: int,
    anchor: str,
    now: datetime | None,
    applied: bool = True,
) -> StepOutput:
    """One learning-plane round for one case. Pure given (case, facts, source results)."""
    out = StepOutput(case)
    cur = case
    spent = lifetime_spent
    p_bad, p_good = hypotheses(ref, params.margin)
    token_cap = int(1.5 * params.max_trials * ref.mean_tokens)

    def move(to: RevalState, trig: RevalTrigger, meas: dict[str, float] | None = None,
             detail: Sequence[str] = (), rebase: dict[str, str] | None = None,
             **upd: object) -> None:  # fmt: skip
        nonlocal cur
        seq = cur.seq + 1
        m = {k: round(v, 6) for k, v in (meas or {}).items()}
        out.transitions.append(
            RevalTransition(
                cell_key=cur.cell_key,
                episode=cur.episode,
                cycle=cur.cycle,
                seq=seq,
                from_state=cur.state,
                to_state=to,
                trigger=trig,
                reason=cur.reason,
                strategy=params.strategy,
                measurements=m,
                detail=list(detail),
                facts=facts.model_dump(mode="json") if facts is not None else {},
                rebase=rebase or {},
                n_trials=cur.n,
                n_unverified=cur.n_unverified,
                tokens_cycle=cur.tokens_cycle,
                anchor=anchor,
                observed_at=now,
                params_hash=params.config_hash(),
                applied=applied,
            )
        )
        cur = replace(cur, state=to, seq=seq, **upd)  # type: ignore[arg-type]

    def evidence_meas() -> dict[str, float]:
        mean_tokens = cur.tokens_counted / cur.n if cur.n else 0.0
        return {
            "p_ref": ref.p_alt, "p_bad": p_bad, "p_good": p_good, "alpha": cur.alpha,
            "beta": params.beta, "k": float(cur.k), "n": float(cur.n),
            "unverified": float(cur.n_unverified), "plan_n": float(cur.plan_n),
            "k_crit": float(cur.plan_k), "llr": cur.llr, "tokens_cycle": float(cur.tokens_cycle),
            "mean_tokens": mean_tokens, "ref_mean_tokens": ref.mean_tokens,
            "first_ordinal": float(cur.first_ordinal or 0), "last_ordinal": float(cur.ordinal - 1),
        }  # fmt: skip

    def reset_cycle(**extra: Any) -> dict[str, Any]:
        return {
            "dwell": 0, "default_n": 0, "default_k": 0, "cycle_started_at": None, "ordinal": 0,
            "n": 0, "k": 0, "n_unverified": 0, "n_rejected": 0, "mismatches": 0,
            "tokens_cycle": 0, "tokens_counted": 0, "tokens_sq": 0.0, "llr": 0.0, "versions": None,
            "first_ordinal": None, "last_trial_at": None, "candidate_at": None, "c_ordinal": 0,
            "c_n": 0, "c_k": 0, "c_unverified": 0, "plan_n": 0, "plan_k": 0, **extra,
        }  # fmt: skip

    def fail_cycle(trig: RevalTrigger, detail: Sequence[str] = (), **extra: Any) -> None:
        meas = evidence_meas()
        nxt = cur.cycle + 1
        if nxt >= params.max_cycles:
            move(RevalState.BUDGET_EXHAUSTED, RevalTrigger.CYCLES_EXHAUSTED, meas,
                 [trig.value, *detail], cycle=nxt, **reset_cycle(), **extra)  # fmt: skip
        else:
            move(RevalState.QUARANTINED, trig, meas, detail, cycle=nxt, **reset_cycle(), **extra)

    def lifetime_exhausted() -> bool:
        return spent >= params.cell_token_budget

    # ---- terminal / expiry ----
    if cur.state in TERMINAL_STATES:
        return out
    age = _hours(cur.opened_at, now)
    if age is not None and age > params.case_ttl_hours:
        move(RevalState.EXPIRED, RevalTrigger.TTL_EXPIRED, {"age_hours": age})
        out.case = cur
        return out
    cycle_age = _hours(cur.cycle_started_at, now)
    if (
        cur.state in (RevalState.REVALIDATING, RevalState.RECOVERY_CANDIDATE)
        and cycle_age is not None
        and cycle_age > params.max_cycle_hours
    ):
        move(
            RevalState.EXPIRED,
            RevalTrigger.CYCLE_EXPIRED,
            {**evidence_meas(), "cycle_hours": cycle_age},
        )
        out.case = cur
        return out

    # ---- QUARANTINED: eligibility and readiness ----
    if cur.state == RevalState.QUARANTINED and not cur.inert:
        gates_facts = facts if execute else None
        elig = check_eligibility(
            params, cur, ref, tenant_id=tenant_id, facts=gates_facts, lifetime_spent=spent
        )
        if not elig.eligible:
            if elig.permanent and elig.permanent_trigger in (
                RevalTrigger.CYCLES_EXHAUSTED,
                RevalTrigger.LIFETIME_BUDGET,
            ):
                move(RevalState.BUDGET_EXHAUSTED, elig.permanent_trigger, {}, elig.reasons)
            elif elig.reasons != cur.last_ineligible:
                move(RevalState.QUARANTINED, RevalTrigger.INELIGIBLE, {}, elig.reasons,
                     last_ineligible=elig.reasons, inert=elig.permanent)  # fmt: skip
        else:
            ready, rmeas = strategy_ready(params, cur, ref)
            if ready:
                move(RevalState.REVALIDATION_PENDING, RevalTrigger.READY, rmeas,
                     last_ineligible=())  # fmt: skip

    # ---- PENDING: start the cycle ----
    if cur.state == RevalState.REVALIDATION_PENDING and execute and source is not None:
        elig = check_eligibility(params, cur, ref, tenant_id=tenant_id, facts=facts,
                                 lifetime_spent=spent)  # fmt: skip
        if not elig.eligible and elig.permanent_trigger in (
            RevalTrigger.CYCLES_EXHAUSTED,
            RevalTrigger.LIFETIME_BUDGET,
        ):
            move(RevalState.BUDGET_EXHAUSTED, elig.permanent_trigger, {}, elig.reasons)
        elif elig.eligible:
            alpha = case_alpha(params, cur)
            plan = plan_fixed(p_bad, p_good, alpha, params.beta, params.max_trials)
            assert plan is not None  # eligibility checked the plan
            move(RevalState.REVALIDATING, RevalTrigger.TRIALS_STARTED,
                 {"plan_n": float(plan.n), "k_crit": float(plan.k_crit), "alpha": alpha,
                  "plan_size": plan.size, "plan_power": plan.power, "p_bad": p_bad,
                  "p_good": p_good},
                 cycle_started_at=now, alpha=alpha, plan_n=plan.n, plan_k=plan.k_crit)  # fmt: skip

    if not execute or source is None:
        out.case = cur
        return out

    # ---- trials ----
    j = 0
    spacing = params.round_hours / params.trials_per_round

    def run_one(stage: TrialStage) -> tuple[TrialResult, bool, RejectReason | None] | None:
        nonlocal cur, spent, j
        ordinal = cur.ordinal if stage == TrialStage.EVIDENCE else cur.c_ordinal
        key = TrialKey(cell_key=cur.cell_key, episode=cur.episode, cycle=cur.cycle,
                       ordinal=ordinal, stage=stage)  # fmt: skip
        at = (now or datetime.min) + timedelta(hours=j * spacing)
        res = source.run(ref, key, at)
        if res is None:
            return None
        j += 1
        reject: RejectReason | None = None
        if res.tenant_id is not None and res.tenant_id != tenant_id:
            reject = RejectReason.TENANT_MISMATCH
        elif cur.trigger_at is not None and res.observed_at <= cur.trigger_at:
            reject = RejectReason.NOT_NEWER
        elif res.task_ref in cur.task_refs:
            reject = RejectReason.DUPLICATE
        elif (
            cur.reason != QuarantineReason.STRUCTURAL_INVALIDATION
            and _conflict(ref, res)
            or (
                cur.reason == QuarantineReason.STRUCTURAL_INVALIDATION
                and cur.versions is not None
                and cur.versions != (res.runtime_version, res.verifier_version)
            )
        ):
            reject = RejectReason.VERSION_MISMATCH
        out.trials.append(
            RevalTrial(
                key=key,
                result=res,
                anchor=anchor,
                round_at=now,
                accepted=reject is None,
                reject_reason=reject,
            )
        )
        out.tokens += res.tokens
        spent += res.tokens
        upd: dict[str, object] = {"tokens_cycle": cur.tokens_cycle + res.tokens}
        if stage == TrialStage.EVIDENCE:
            upd["ordinal"] = ordinal + 1
            if cur.first_ordinal is None:
                upd["first_ordinal"] = ordinal
        else:
            upd["c_ordinal"] = ordinal + 1
        if reject is None:
            upd["task_refs"] = cur.task_refs | {res.task_ref}
            upd["last_trial_at"] = res.observed_at
            if cur.versions is None and cur.reason == QuarantineReason.STRUCTURAL_INVALIDATION:
                upd["versions"] = (res.runtime_version, res.verifier_version)
            if stage == TrialStage.EVIDENCE:
                if res.counted:
                    upd.update(n=cur.n + 1, k=cur.k + int(res.success),
                               tokens_counted=cur.tokens_counted + res.tokens,
                               tokens_sq=cur.tokens_sq + float(res.tokens) ** 2)  # fmt: skip
                    upd["llr"] = cur.llr + sprt_step(p_bad, p_good, res.success)
                else:
                    upd["n_unverified"] = cur.n_unverified + 1
            elif res.counted:
                upd.update(c_n=cur.c_n + 1, c_k=cur.c_k + int(res.success))
            else:
                upd["c_unverified"] = cur.c_unverified + 1
        else:
            upd["n_rejected"] = cur.n_rejected + 1
            if reject == RejectReason.VERSION_MISMATCH:
                upd["mismatches"] = cur.mismatches + 1
        cur = replace(cur, **upd)  # type: ignore[arg-type]
        return res, reject is None, reject

    if cur.state == RevalState.REVALIDATING:
        lo, hi = sprt_bounds(cur.alpha, params.beta)
        while j < params.trials_per_round and cur.state == RevalState.REVALIDATING:
            if cur.ordinal >= params.max_trials:
                move(RevalState.BUDGET_EXHAUSTED, RevalTrigger.TRIAL_CAP, evidence_meas())
                break
            if cur.tokens_cycle >= token_cap:
                move(RevalState.BUDGET_EXHAUSTED, RevalTrigger.TOKEN_CAP, evidence_meas())
                break
            if lifetime_exhausted():
                move(RevalState.BUDGET_EXHAUSTED, RevalTrigger.LIFETIME_BUDGET, evidence_meas())
                break
            got = run_one(TrialStage.EVIDENCE)
            if got is None:
                break
            total = cur.n + cur.n_unverified
            if (
                total >= params.min_unverified_trials
                and cur.n_unverified / total > params.max_unverified_fraction
            ):
                fail_cycle(RevalTrigger.VERIFICATION_UNAVAILABLE,
                           [f"{cur.n_unverified}/{total} trials unverified"])  # fmt: skip
                break
            if cur.mismatches >= params.lineage_mismatch_limit:
                fail_cycle(RevalTrigger.LINEAGE_VIOLATION,
                           [f"{cur.mismatches} trials reported versions that do not match"],
                           reason=QuarantineReason.STRUCTURAL_INVALIDATION)  # fmt: skip
                break
            mean = cur.tokens_counted / cur.n if cur.n else 0.0
            if cur.n >= params.cost_futility_min_trials:
                # a cost problem is visible long before the success test can finish; stopping on
                # it can only end a cycle earlier with a failure, never produce a recovery
                var = max(0.0, (cur.tokens_sq - cur.n * mean * mean) / (cur.n - 1))
                lower = mean - normal_quantile(params.cost_futility_confidence) * math.sqrt(
                    var / cur.n
                )
                if lower > ref.mean_tokens * params.cost_guard:
                    fail_cycle(RevalTrigger.CRITERIA_NOT_MET, ["cost futility"])
                    break
            cost_ok = cur.n > 0 and mean <= ref.mean_tokens * params.cost_guard
            if params.strategy == RevalStrategy.SEQUENTIAL:
                if cur.llr >= hi:
                    if cost_ok:
                        move(RevalState.RECOVERY_CANDIDATE, RevalTrigger.CRITERIA_MET,
                             evidence_meas(), candidate_at=cur.last_trial_at)  # fmt: skip
                    else:
                        fail_cycle(RevalTrigger.CRITERIA_NOT_MET, ["cost guard failed"])
                elif cur.llr <= lo:
                    fail_cycle(RevalTrigger.CRITERIA_NOT_MET, ["SPRT accepted p <= p_bad"])
            elif cur.n >= cur.plan_n:
                if cur.k >= cur.plan_k and cost_ok:
                    move(RevalState.RECOVERY_CANDIDATE, RevalTrigger.CRITERIA_MET,
                         evidence_meas(), candidate_at=cur.last_trial_at)  # fmt: skip
                else:
                    why = "success criterion not met" if cur.k < cur.plan_k else "cost guard failed"
                    fail_cycle(RevalTrigger.CRITERIA_NOT_MET, [why])

    # ---- candidate: wait the gap, confirm, restore ----
    if cur.state == RevalState.RECOVERY_CANDIDATE:
        waited = _hours(cur.candidate_at, now)
        gap = params.confirm_gap_hours if params.confirm_trials > 0 else 0.0
        if waited is None or waited >= gap:
            while (
                params.confirm_trials > 0
                and j < params.trials_per_round
                and (cur.c_n + cur.c_unverified < params.confirm_trials)
            ):
                if lifetime_exhausted():
                    move(RevalState.BUDGET_EXHAUSTED, RevalTrigger.LIFETIME_BUDGET, evidence_meas())
                    break
                if run_one(TrialStage.CONFIRMATION) is None:
                    break
                if cur.mismatches >= params.lineage_mismatch_limit:
                    fail_cycle(RevalTrigger.LINEAGE_VIOLATION, ["confirmation versions mismatch"],
                               reason=QuarantineReason.STRUCTURAL_INVALIDATION)  # fmt: skip
                    break
            if cur.state == RevalState.RECOVERY_CANDIDATE and (
                params.confirm_trials == 0 or cur.c_n + cur.c_unverified >= params.confirm_trials
            ):
                total = cur.c_n + cur.c_unverified
                meas = {
                    **evidence_meas(),
                    "confirm_n": float(cur.c_n),
                    "confirm_k": float(cur.c_k),
                    "confirm_unverified": float(cur.c_unverified),
                }
                if total and cur.c_unverified / total > params.max_unverified_fraction:
                    fail_cycle(RevalTrigger.VERIFICATION_UNAVAILABLE, ["confirmation unverified"])
                elif cur.c_n and binom_cdf(cur.c_k, cur.c_n, p_bad) <= params.confirm_alpha:
                    meas["confirm_tail"] = binom_cdf(cur.c_k, cur.c_n, p_bad)
                    fail_cycle(RevalTrigger.CONFIRMATION_FAILED, ["collapse after the evidence"])
                else:
                    rebase: dict[str, str] = {}
                    if cur.reason == QuarantineReason.STRUCTURAL_INVALIDATION and cur.versions:
                        rebase = {
                            "runtime_version": cur.versions[0] or "",
                            "verifier_version": cur.versions[1] or "",
                        }
                    elif cur.reason == QuarantineReason.EVIDENCE_STALE and cur.last_trial_at:
                        rebase = {"recorded_at": cur.last_trial_at.isoformat()}
                    move(RevalState.RECOVERED, RevalTrigger.CONFIRMED, meas, rebase=rebase)
                    out.restored = out.transitions[-1]
    out.case = cur
    return out


# ---- the fleet ---------------------------------------------------------------------------------
class RevalidationFleet:
    """The M6.2 monitor fleet plus one revalidation case per quarantined cell.

    Native recovery is disabled for an open case through the existing `MonitorState.locked` flag;
    only RECOVERED returns the cell to NORMAL (windows reset, sequence and episode preserved).
    """

    def __init__(
        self,
        refs: Sequence[CellReference],
        drift: DriftParams,
        params: RevalidationParams,
        tenant_id: str | None = None,
    ):
        self.params = params
        self.drift = drift
        self.tenant_id = tenant_id
        self.monitor = MonitorFleet(refs, drift, tenant_id)
        self.cases: dict[str, CaseState] = {}
        self.spent: dict[str, int] = {}
        self.transitions: list[RevalTransition] = []
        self.trials: list[RevalTrial] = []
        self._rseq: dict[str, int] = {}

    @property
    def execute(self) -> bool:
        return self.drift.mode == DriftMode.ENFORCE

    # -- observations --
    def ingest(self, records: Iterable[DecisionOutcomeRecord]) -> list[DriftTransition]:
        drift_new: list[DriftTransition] = []
        for rec in records:
            if not rec.acted or (self.tenant_id is not None and rec.tenant_id != self.tenant_id):
                continue
            ts = self.monitor.ingest([rec])
            drift_new += ts
            for t in ts:
                if (
                    t.to_state == DriftState.QUARANTINED
                    and t.trigger == DriftTrigger.QUARANTINE_APPLIED
                ):
                    self._open(t, ts)
            if rec.known:
                self._observe_default(rec)
        return drift_new

    def _observe_default(self, rec: DecisionOutcomeRecord) -> None:
        for cell, case in self.cases.items():
            if case.state in TERMINAL_STATES:
                continue
            ref = self.monitor.refs[cell]
            if (rec.family, rec.risk_level, rec.default_key, rec.action) == (
                ref.family, ref.risk, ref.default_key, ref.default_key,
            ):  # fmt: skip
                self.cases[cell] = replace(
                    case,
                    dwell=case.dwell + 1,
                    default_n=case.default_n + 1,
                    default_k=case.default_k + int(rec.success),
                )

    def _open(self, t: DriftTransition, batch: Sequence[DriftTransition]) -> None:
        reason, severity = reason_and_severity(t, batch, self.drift.detector)
        st = self.monitor.states[t.cell_key]
        self.monitor.states[t.cell_key] = replace(st, locked=True)  # native recovery off
        seq = self._rseq.get(t.cell_key, 0)
        case = CaseState(
            cell_key=t.cell_key,
            episode=st.episode,
            reason=reason,
            severity=severity,
            trigger_decision_id=t.decision_id,
            trigger_at=t.observed_at,
            opened_at=t.observed_at,
            seq=seq,
            alpha=self.params.alpha_severe
            if severity >= self.params.severe_drop
            else self.params.alpha,
        )
        self.cases[t.cell_key] = case
        self._record(
            RevalTransition(
                cell_key=t.cell_key, episode=case.episode, cycle=0, seq=seq + 1, from_state=None,
                to_state=RevalState.QUARANTINED, trigger=RevalTrigger.CASE_OPENED, reason=reason,
                strategy=self.params.strategy,
                measurements={"severity": round(severity, 6)}, anchor=t.decision_id,
                observed_at=t.observed_at, params_hash=self.params.config_hash(),
                applied=self.drift.mode == DriftMode.ENFORCE,
            )
        )  # fmt: skip
        self.cases[t.cell_key] = replace(case, seq=seq + 1)

    def _record(self, tr: RevalTransition) -> None:
        self._rseq[tr.cell_key] = tr.seq
        self.transitions.append(tr)

    # -- a learning-plane round --
    def step(
        self, source: TrialSource | None, anchor: str, now: datetime | None
    ) -> list[RevalTransition]:
        new: list[RevalTransition] = []
        for cell in sorted(self.cases):
            case = self.cases[cell]
            if case.state in TERMINAL_STATES:
                continue
            ref = self.monitor.refs[cell]
            facts = (
                source.facts(ref, now or datetime.min)
                if source is not None and self.execute
                else None
            )
            res = step_case(
                ref, self.params, case, execute=self.execute, tenant_id=self.tenant_id or "",
                source=source, facts=facts, lifetime_spent=self.spent.get(cell, 0), anchor=anchor,
                now=now, applied=self.execute,
            )  # fmt: skip
            self.cases[cell] = res.case
            self.spent[cell] = self.spent.get(cell, 0) + res.tokens
            self.trials.extend(res.trials)
            for tr in res.transitions:
                self._record(tr)
            new += res.transitions
            if res.restored is not None:
                self._restore(cell, res.restored)
        return new

    def _restore(self, cell: str, tr: RevalTransition) -> None:
        self.monitor.restore(cell)
        if tr.rebase:
            ref = self.monitor.refs[cell]
            upd: dict[str, object] = {}
            if "runtime_version" in tr.rebase:
                upd["runtime_version"] = tr.rebase["runtime_version"] or None
                upd["verifier_version"] = tr.rebase["verifier_version"] or None
            if "recorded_at" in tr.rebase:
                upd["recorded_at"] = datetime.fromisoformat(tr.rebase["recorded_at"])
            self.monitor.replace_reference(replace(ref, **upd))  # type: ignore[arg-type]

    def overlay(self) -> DriftOverlay:
        return self.monitor.overlay()


def cell_reference_of(
    key: str, cell: PairedCell, runtime: str | None = None, verifier: str | None = None
) -> CellReference:
    """Convenience for tests: a reference without a registry source."""
    from experience_engine.controller.drift import parse_paired_key

    family, risk, default, alt = parse_paired_key(key)
    return CellReference(
        cell_key=key, family=family, risk=risk, default_key=default, alt_key=alt, n=cell.n,
        k_alt=cell.both + cell.alt_only, k_def=cell.both + cell.default_only,
        tokens_alt=cell.alt_tokens, tokens_def=cell.default_tokens, runtime_version=runtime,
        verifier_version=verifier,
    )  # fmt: skip


# ---- replay ------------------------------------------------------------------------------------
class ReplaySource:
    """Serves persisted trials (by case, cycle, stage and ordinal) and the executor facts recorded
    on persisted transitions. It never executes anything: a request with no persisted trial
    returns None, so a replay stops exactly where the stored evidence stops."""

    def __init__(
        self,
        trials: Iterable[RevalTrial],
        transitions: Iterable[RevalTransition],
        tenant_id: str,
    ):
        self._tenant = tenant_id
        self._trials = {t.key.ident(): t.result for t in trials}
        self._facts: dict[str, list[tuple[datetime, ExecutorFacts]]] = {}
        for tr in sorted(transitions, key=lambda x: (x.observed_at or datetime.min, x.seq)):
            if tr.facts and tr.observed_at is not None:
                self._facts.setdefault(tr.cell_key, []).append(
                    (tr.observed_at, ExecutorFacts.model_validate(tr.facts))
                )

    def facts(self, ref: CellReference, now: datetime) -> ExecutorFacts:
        known = [f for at, f in self._facts.get(ref.cell_key, []) if at <= now]
        return known[-1] if known else ExecutorFacts(tenant_id=self._tenant)

    def run(self, ref: CellReference, key: TrialKey, at: datetime) -> TrialResult | None:
        return self._trials.get(key.ident())


def replay_revalidation(
    refs: Sequence[CellReference],
    drift: DriftParams,
    params: RevalidationParams,
    tenant_id: str | None,
    records: Sequence[DecisionOutcomeRecord],
    transitions: Sequence[RevalTransition],
    trials: Sequence[RevalTrial],
) -> RevalidationFleet:
    """Recompute the fleet from the ordered outcomes and the persisted evidence only."""
    fleet = RevalidationFleet(refs, drift, params, tenant_id)
    source = ReplaySource(trials, transitions, tenant_id or "")
    index = {r.decision_id: n for n, r in enumerate(records)}
    pos = 0
    for anchor, now in _round_times(transitions, trials):
        upto = index[anchor] + 1 if anchor in index else 0
        fleet.ingest(records[pos:upto])
        pos = max(pos, upto)
        fleet.step(source, anchor, now)
    fleet.ingest(records[pos:])
    return fleet


def _round_times(
    transitions: Sequence[RevalTransition], trials: Sequence[RevalTrial]
) -> list[tuple[str, datetime]]:
    """Rounds that left evidence, as (anchor, round start), in time order. A transition is stamped
    with its round's start and a trial carries it in `round_at`; case openings are not rounds
    (they are derived from observations)."""
    rounds: set[tuple[str, datetime]] = set()
    for tr in transitions:
        if tr.trigger != RevalTrigger.CASE_OPENED and tr.observed_at is not None:
            rounds.add((tr.anchor, tr.observed_at))
    for t in trials:
        rounds.add((t.anchor, t.round_at or t.result.observed_at))
    return sorted(rounds, key=lambda r: (r[1], r[0]))
