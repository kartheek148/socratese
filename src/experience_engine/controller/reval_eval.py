"""Simulated revalidation executor, variants and scoring for the M6.3 experiments (deterministic).

Variants share one stream (tasks, prior, perturbations, common random numbers):
  m61         M6.1 coarse guard
  m62         M6.2 ENFORCE, native probe recovery
  m62_locked  M6.2 ENFORCE with recovery disabled (permanent quarantine; zero probes)
  m63_A/B/C   M6.2 ENFORCE + controlled revalidation (native recovery disabled, no live probes)
Ground truth: `Stream.degraded` (performance only), per family and position.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from experience_engine.controller.drift import CellReference
from experience_engine.controller.drift_eval import (
    UTILITY_LAMBDA,
    drift_params_for,
    position_of,
)
from experience_engine.controller.scenarios import Scenario, Stream, build_stream, make_world
from experience_engine.controller.simulate import (
    SIM_EPOCH,
    SIM_VERIFIER,
    SimConfig,
    SimRun,
    TaskOutcome,
    run_stream,
)
from experience_engine.domain.controller import ControllerKind, ControllerParams
from experience_engine.domain.drift import DriftMode, DriftParams, DriftTrigger
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.revalidation import (
    ExecutorFacts,
    RevalidationParams,
    RevalState,
    RevalStrategy,
    RevalTrigger,
    TrialKey,
    TrialResult,
    TrialVerdict,
)

REVAL_VARIANTS = ("m61", "m62", "m62_locked", "m63_A", "m63_B", "m63_C")
STRATEGY_OF = {
    "m63_A": RevalStrategy.FIXED,
    "m63_B": RevalStrategy.EVIDENCE_THRESHOLD,
    "m63_C": RevalStrategy.SEQUENTIAL,
}
POOL_SIZE = 1600
OUTAGE_FRACTION_LIMIT = 0.20


def hours_since_epoch(at: datetime) -> int:
    return int((at - SIM_EPOCH).total_seconds() // 3600)


@lru_cache(maxsize=8)
def _pool_for(families: tuple[Any, ...], seed: int, size: int) -> list[TaskOutcome]:
    return make_world(families, size, seed, tag="rv")


class SimExecutor:
    """Out-of-band trials of the alternative drawn from an independent pool of the world.

    A trial observed at time t sees the world as it is at t (the same perturbation as production
    at that position) but uses its own random draw index, so trials and production decisions never
    share outcomes. The revalidation verifier can be down over a window (scenario R4): such a
    trial still costs tokens and is returned as VERIFICATION_UNAVAILABLE."""

    def __init__(self, stream: Stream, tenant_id: str = "bench", pool_size: int = POOL_SIZE):
        self.stream = stream
        self.tenant_id = tenant_id
        self._pool = _pool_for(stream.scenario.families, stream.seed, pool_size)
        self._next = 0

    def _take(self, family: str | None) -> tuple[int, TaskOutcome] | None:
        while self._next < len(self._pool):
            idx = self._next
            self._next += 1
            if self._pool[idx].family == family:
                return idx, self._pool[idx]
        return None

    def facts(self, ref: CellReference, now: datetime) -> ExecutorFacts:
        runtime = self.stream.perturbation.apply(
            hours_since_epoch(now), ref.family, True, 1, draw=-1
        )[2]
        left = sum(1 for t in self._pool[self._next :] if t.family == ref.family)
        return ExecutorFacts(
            tenant_id=self.tenant_id,
            supported=True,
            runtime_version=runtime,
            verifier_version=SIM_VERIFIER,
            verification_available=True,
            pool=left,
        )

    def run(self, ref: CellReference, key: TrialKey, at: datetime) -> TrialResult | None:
        taken = self._take(ref.family)
        if taken is None:
            return None
        idx, task = taken
        status, tokens = task.outcomes[ref.alt_key]
        i = hours_since_epoch(at)
        ok, tokens, runtime = self.stream.perturbation.apply(
            i, ref.family, status == OutcomeStatus.VERIFIED_SUCCESS, tokens, draw=1_000_000 + idx
        )
        out = self.stream.scenario.verifier_outage
        if out is not None and out[0] <= i < out[1]:
            verdict = TrialVerdict.VERIFICATION_UNAVAILABLE
        else:
            verdict = TrialVerdict.SUCCESS if ok else TrialVerdict.FAILURE
        return TrialResult(
            verdict=verdict,
            tokens=tokens,
            task_ref=f"rv-{idx}",
            observed_at=at,
            runtime_version=runtime,
            verifier_version=SIM_VERIFIER,
            capability_ref=ref.alt_key,
            tenant_id=self.tenant_id,
        )


def reval_config(
    variant: str,
    stream: Stream,
    drift_base: DriftParams,
    reval_base: RevalidationParams,
    refresh_every: int = 10,
) -> SimConfig:
    raw: dict[str, Any] = {
        "evidence": {},
        "exploration_risk_levels": ["low"],
        "exploration_families": sorted({f.name for f in stream.scenario.families}),
        "epsilon": 0.10,
        "min_total_verified": 100,
    }
    executor: SimExecutor | None = None
    if variant != "m61":
        dp = drift_params_for(stream.scenario, drift_base, DriftMode.ENFORCE)
        if variant == "m62_locked":
            dp = dp.model_copy(update={"min_quarantine": 10**9})
        raw["drift"] = dp.model_dump(mode="json")
    if variant in STRATEGY_OF:
        raw["revalidation"] = reval_base.model_copy(
            update={"strategy": STRATEGY_OF[variant]}
        ).model_dump(mode="json")
        executor = SimExecutor(stream)
    return SimConfig(
        ControllerKind.EVIDENCE_DRIVEN,
        ControllerParams.model_validate(raw),
        prior=stream.prior,
        refresh_every=refresh_every,
        perturb=stream.perturbation,
        perturb_default=stream.default_perturbation,
        executor=executor,
    )


@dataclass
class RevalResult:
    sid: str
    seed: int
    variant: str
    expect: str
    n: int
    successes: int = 0
    tokens: int = 0
    rule_successes: int = 0
    rule_tokens: int = 0
    reval_tokens: int = 0
    reval_trials: int = 0
    reval_unavailable: int = 0
    reval_rejected: int = 0
    probe_tokens: int = 0
    n_probe: int = 0
    n_alt: int = 0
    exposure_degraded: int = 0  # decisions that ran the alternative while it was degraded
    fallback_decisions: int = 0
    quarantines: int = 0
    first_quarantine: int | None = None
    restorations: list[int] = field(default_factory=list)
    false_restorations: int = 0
    unverified_evidence_restorations: int = 0
    cycles_started: int = 0
    cycles_failed: int = 0
    terminal: str | None = None
    final_state: str = "NORMAL"
    degraded_first: int | None = None
    degraded_end: int | None = None  # first position after the last degraded one (None: never ends)
    prior_draws: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def restored(self) -> bool:
        return bool(self.restorations)

    @property
    def utility(self) -> float:
        return self.successes - (self.tokens + self.reval_tokens) * UTILITY_LAMBDA

    @property
    def rule_utility(self) -> float:
        return self.rule_successes - self.rule_tokens * UTILITY_LAMBDA


def _round_position(at: datetime | None, fallback: int) -> int:
    return hours_since_epoch(at) if at is not None else fallback


def score_reval(stream: Stream, variant: str, run: SimRun) -> RevalResult:
    sc = stream.scenario
    fam = sc.families[0].name
    truth = stream.degraded[fam]
    n = len(run.steps)
    res = RevalResult(sc.sid, stream.seed, variant, sc.expect, n, prior_draws=stream.prior_draws)
    res.successes = sum(int(s.success) for s in run.steps)
    res.tokens = sum(s.tokens for s in run.steps)
    res.rule_successes = sum(int(s.default_success) for s in run.steps)
    res.rule_tokens = sum(s.default_tokens for s in run.steps)
    res.n_alt = sum(1 for s in run.steps if s.deviated)
    res.n_probe = sum(1 for s in run.steps if s.probed)
    res.probe_tokens = sum(s.tokens for s in run.steps if s.probed)
    res.exposure_degraded = sum(1 for s in run.steps if s.deviated and truth[s.index])
    res.fallback_decisions = sum(
        1 for s in run.steps if s.drift_state in ("DRIFTED", "QUARANTINED", "RECOVERING")
    )
    idx = [i for i, d in enumerate(truth) if d]
    if idx:
        res.degraded_first = idx[0]
        end = idx[-1] + 1
        res.degraded_end = end if end < n else None
    q = [
        t
        for t in run.transitions
        if t.to_state.value == "QUARANTINED" and t.trigger == DriftTrigger.QUARANTINE_APPLIED
    ]
    res.quarantines = len(q)
    res.first_quarantine = position_of(q[0].decision_id) if q else None
    if variant.startswith("m63"):
        res.reval_tokens = run.reval_tokens
        res.reval_trials = len(run.reval_trials)
        res.reval_unavailable = sum(
            1 for t in run.reval_trials if t.result.verdict == TrialVerdict.VERIFICATION_UNAVAILABLE
        )
        res.reval_rejected = sum(1 for t in run.reval_trials if not t.accepted)
        res.cycles_started = sum(
            1 for t in run.reval_transitions if t.trigger == RevalTrigger.TRIALS_STARTED
        )
        failed = {
            RevalTrigger.CRITERIA_NOT_MET,
            RevalTrigger.CONFIRMATION_FAILED,
            RevalTrigger.VERIFICATION_UNAVAILABLE,
            RevalTrigger.LINEAGE_VIOLATION,
        }
        res.cycles_failed = sum(1 for t in run.reval_transitions if t.trigger in failed)
        res.reasons = [
            t.reason.value for t in run.reval_transitions if t.trigger == RevalTrigger.CASE_OPENED
        ]
        for t in run.reval_transitions:
            if t.to_state == RevalState.RECOVERED:
                pos = min(_round_position(t.observed_at, 0), n - 1)
                res.restorations.append(pos)
                res.false_restorations += int(truth[pos])
                tot = t.measurements.get("n", 0.0) + t.measurements.get("unverified", 0.0)
                if tot and t.measurements.get("unverified", 0.0) / tot > OUTAGE_FRACTION_LIMIT:
                    res.unverified_evidence_restorations += 1
        if run.reval_transitions:
            last = run.reval_transitions[-1]
            res.terminal = last.to_state.value if last.to_state in (
                RevalState.EXPIRED, RevalState.BUDGET_EXHAUSTED, RevalState.RECOVERED
            ) else None  # fmt: skip
    elif variant == "m62":
        for dt in run.transitions:
            if dt.trigger == DriftTrigger.RECOVERY_CRITERIA_MET:
                pos = min(position_of(dt.decision_id), n - 1)
                res.restorations.append(pos)
                res.false_restorations += int(truth[pos])
    res.final_state = next(iter(run.final_states.values()), "NORMAL")
    return res


def run_reval_stream(
    scenario: Scenario,
    seed: int,
    drift_base: DriftParams,
    reval_base: RevalidationParams,
    variants: Sequence[str] = REVAL_VARIANTS,
    refresh_every: int = 10,
) -> list[RevalResult]:
    stream = build_stream(scenario, seed)
    order = list(range(scenario.n))
    out: list[RevalResult] = []
    for v in variants:
        cfg = reval_config(v, stream, drift_base, reval_base, refresh_every)
        run = run_stream(stream.tasks, order, cfg, label=f"{scenario.sid}-{seed}-{v}")
        out.append(score_reval(stream, v, run))
    return out


def result_to_json(r: RevalResult) -> dict[str, Any]:
    return asdict(r)


def result_from_json(d: dict[str, Any]) -> RevalResult:
    return RevalResult(**d)
