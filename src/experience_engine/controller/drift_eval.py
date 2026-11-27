"""Scoring, calibration and aggregation of the M6.2 drift experiments (pure, deterministic).

Two evaluation paths share the scenarios:
* monitor-only: the alternative is executed at every position (what SHADOW observes), the monitor
  folds the perturbed outcomes; used for calibration and the detector comparison (fast);
* controller-in-the-loop: rule baseline, M6.1 (coarse guard), M6.2 SHADOW and M6.2 ENFORCE run on
  the same stream with common random numbers; used for economics, fallback and recovery.
"""

import math
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from experience_engine.controller.drift import (
    Observation,
    parse_paired_key,
    references_from_snapshot,
    replay,
)
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.scenarios import (
    CAP_KEY,
    Scenario,
    Stream,
    build_stream,
)
from experience_engine.controller.simulate import (
    SIM_EPOCH,
    SIM_VERIFIER,
    SimConfig,
    SimStep,
    run_stream,
)
from experience_engine.domain.controller import (
    PLANNING_KEY,
    ControllerKind,
    ControllerParams,
)
from experience_engine.domain.drift import (
    PRIOR_DISABLED,
    DriftMode,
    DriftParams,
    DriftState,
    DriftTransition,
    DriftTrigger,
)
from experience_engine.evaluation.paired import bootstrap_mean_ci

VARIANTS = ("m61", "m62_shadow", "m62")
SUCCESS_TOLERANCE = 0.10
COST_TOLERANCE = 0.30
UTILITY_LAMBDA = 1.0 / 20000.0  # utility per task = success - lambda * tokens (raw columns kept)


def position_of(decision_id: str) -> int:
    return int(decision_id.rsplit("-", 1)[1])


def drift_params_for(scenario: Scenario, base: DriftParams, mode: DriftMode) -> DriftParams:
    update: dict[str, Any] = {"mode": mode}
    if scenario.max_age_days is not None:
        update["max_evidence_age_days"] = scenario.max_age_days
    return base.model_copy(update=update)


def controller_config(
    variant: str, stream: Stream, base: DriftParams, refresh_every: int = 10
) -> SimConfig:
    raw: dict[str, Any] = {
        "evidence": {},
        "exploration_risk_levels": ["low"],
        "exploration_families": sorted({f.name for f in stream.scenario.families}),
        "epsilon": 0.10,
        "min_total_verified": 100,
    }
    if variant in ("m62_shadow", "m62"):
        mode = DriftMode.ENFORCE if variant == "m62" else DriftMode.SHADOW
        raw["drift"] = drift_params_for(stream.scenario, base, mode).model_dump(mode="json")
    return SimConfig(
        ControllerKind.EVIDENCE_DRIVEN,
        ControllerParams.model_validate(raw),
        prior=stream.prior,
        refresh_every=refresh_every,
        perturb=stream.perturbation,
    )


# ---- monitor-only path --------------------------------------------------------------------------
def alt_observations(stream: Stream) -> dict[str, list[Observation]]:
    """What the monitor sees per family when the alternative runs at every position."""
    out: dict[str, list[Observation]] = {f.name: [] for f in stream.scenario.families}
    for i, t in enumerate(stream.tasks):
        status, tokens = t.outcomes[PLANNING_KEY]
        ok, tokens, runtime = stream.perturbation.apply(
            i, t.family, status.value == "VERIFIED_SUCCESS", tokens
        )
        out[t.family or ""].append(
            Observation(
                decision_id=f"o-{i}",
                action=PLANNING_KEY,
                known=True,
                success=ok,
                tokens=tokens,
                at=SIM_EPOCH + timedelta(hours=i),
                runtime_version=runtime,
                verifier_version=SIM_VERIFIER,
            )
        )
    return out


def monitor_only(
    stream: Stream, params: DriftParams
) -> tuple[list[DriftTransition], dict[str, float]]:
    """Transitions of every cell for a monitor that observes the alternative at every position."""
    p = drift_params_for(stream.scenario, params, DriftMode.SHADOW)
    snap = build_snapshot([], "bench", ControllerParams.model_validate({"evidence": {}}),
                          prior=stream.prior)  # fmt: skip
    refs = {parse_paired_key(r.cell_key)[0]: r for r in references_from_snapshot(snap)}
    obs = alt_observations(stream)
    transitions: list[DriftTransition] = []
    t0 = time.perf_counter()
    n_obs = 0
    for fam, series in obs.items():
        if fam in refs:
            _, ts = replay(refs[fam], p, series)
            transitions += ts
            n_obs += len(series)
    cpu = (time.perf_counter() - t0) / max(1, n_obs)
    return transitions, {"cpu_s_per_observation": cpu}


# ---- scoring ------------------------------------------------------------------------------------
@dataclass
class FamilyScore:
    family: str
    onset: int | None
    end: int | None  # first position after the last material one
    drifted_positions: list[int] = field(default_factory=list)
    false_alarms: int = 0
    suspect_episodes: int = 0
    first_detection: int | None = None  # first DRIFTED at or after the onset
    delay: int | None = None
    missed: bool = False
    exposed: bool = False  # enough material observations to be expected to detect
    recovered_at: int | None = None
    recovery_delay: int | None = None
    quarantine_episodes: int = 0
    quarantine_duration: int | None = None


def score_family(
    family: str, material: Sequence[bool], transitions: Sequence[DriftTransition], n: int,
    min_exposure: int = 50,
) -> FamilyScore:  # fmt: skip
    idx = [i for i, m in enumerate(material) if m]
    onset = idx[0] if idx else None
    end = idx[-1] + 1 if idx else None
    sc = FamilyScore(family, onset, end)
    mine = [t for t in transitions if parse_paired_key(t.cell_key)[0] == family]
    sc.suspect_episodes = sum(1 for t in mine if t.to_state == DriftState.SUSPECT)
    quarantined_at: int | None = None
    for t in mine:
        pos = position_of(t.decision_id)
        if t.to_state == DriftState.DRIFTED:
            sc.drifted_positions.append(pos)
            if not material[pos]:
                sc.false_alarms += 1
            elif sc.first_detection is None and onset is not None and pos >= onset:
                sc.first_detection = pos
        if t.to_state == DriftState.QUARANTINED and t.trigger == DriftTrigger.QUARANTINE_APPLIED:
            sc.quarantine_episodes += 1
            quarantined_at = quarantined_at if quarantined_at is not None else pos
        if t.trigger == DriftTrigger.RECOVERY_CRITERIA_MET and sc.recovered_at is None:
            sc.recovered_at = pos
            if quarantined_at is not None:
                sc.quarantine_duration = pos - quarantined_at
    if onset is not None and end is not None:
        sc.exposed = (end - onset) >= min_exposure
        if sc.first_detection is not None:
            sc.delay = sc.first_detection - onset
        else:
            sc.missed = sc.exposed
        if sc.recovered_at is not None and end < n:
            sc.recovery_delay = sc.recovered_at - end
    return sc


@dataclass
class StreamResult:
    sid: str
    seed: int
    variant: str  # rule | m61 | m62_shadow | m62 | monitor
    n: int
    successes: int = 0
    tokens: int = 0
    rule_successes: int = 0
    rule_tokens: int = 0
    n_alt: int = 0
    n_probe: int = 0
    n_fallback: int = 0  # decisions made while the cell was quarantined/recovering/drifted
    probe_tokens: int = 0
    # sums split by ground truth at the decision's position: [n, successes, tokens]
    material: list[int] = field(default_factory=lambda: [0, 0, 0])
    nonmaterial: list[int] = field(default_factory=lambda: [0, 0, 0])
    rule_material: list[int] = field(default_factory=lambda: [0, 0, 0])
    rule_nonmaterial: list[int] = field(default_factory=lambda: [0, 0, 0])
    families: list[FamilyScore] = field(default_factory=list)
    transitions: int = 0
    first_enforced: int | None = None  # first decision that read a prior-disabled state
    prior_draws: int = 0
    cpu_s_per_observation: float = 0.0


def _split(steps: Sequence[SimStep], stream: Stream, res: StreamResult) -> None:
    for st in steps:
        mat = stream.material[st.family or ""][st.index]
        a, b = (res.material, res.rule_material) if mat else (res.nonmaterial, res.rule_nonmaterial)
        a[0] += 1
        a[1] += int(st.success)
        a[2] += st.tokens
        b[0] += 1
        b[1] += int(st.default_success)
        b[2] += st.default_tokens


def score_run(
    stream: Stream, variant: str, steps: Sequence[SimStep], transitions: Sequence[DriftTransition]
) -> StreamResult:
    n = len(steps)
    res = StreamResult(stream.scenario.sid, stream.seed, variant, n, prior_draws=stream.prior_draws)
    res.successes = sum(int(s.success) for s in steps)
    res.tokens = sum(s.tokens for s in steps)
    res.rule_successes = sum(int(s.default_success) for s in steps)
    res.rule_tokens = sum(s.default_tokens for s in steps)
    res.n_alt = sum(1 for s in steps if s.chosen == PLANNING_KEY)
    res.n_probe = sum(1 for s in steps if s.probed)
    res.probe_tokens = sum(s.tokens for s in steps if s.probed)
    disabled = {x.value for x in PRIOR_DISABLED}
    res.n_fallback = sum(1 for s in steps if s.drift_state in disabled)
    res.first_enforced = next((s.index for s in steps if s.drift_state in disabled), None)
    _split(steps, stream, res)
    res.transitions = len(transitions)
    for fam in stream.scenario.families:
        res.families.append(score_family(fam.name, stream.material[fam.name], transitions, n))
    return res


def run_stream_variants(
    scenario: Scenario, seed: int, base: DriftParams, variants: Sequence[str] = VARIANTS,
    refresh_every: int = 10,
) -> list[StreamResult]:  # fmt: skip
    stream = build_stream(scenario, seed)
    order = list(range(scenario.n))
    results: list[StreamResult] = []
    for v in variants:
        if v == "monitor":
            ts, extra = monitor_only(stream, base)
            alt_steps = [
                SimStep(
                    i, t.task_id, t.family, t.risk, PLANNING_KEY, CAP_KEY, "GREEDY", 1.0,
                    *_alt_outcome(stream, i), *t.outcomes[CAP_KEY],
                )
                for i, t in enumerate(stream.tasks)
            ]  # fmt: skip
            r = score_run(stream, v, alt_steps, ts)
            r.cpu_s_per_observation = extra["cpu_s_per_observation"]
            results.append(r)
            continue
        cfg = controller_config(v, stream, base, refresh_every)
        run = run_stream(stream.tasks, order, cfg, label=f"{scenario.sid}-{seed}-{v}")
        results.append(score_run(stream, v, run.steps, run.transitions))
    return results


def _alt_outcome(stream: Stream, i: int) -> tuple[Any, int]:
    from experience_engine.domain.outcome import OutcomeStatus

    t = stream.tasks[i]
    status, tokens = t.outcomes[PLANNING_KEY]
    ok, tokens, _ = stream.perturbation.apply(
        i, t.family, status == OutcomeStatus.VERIFIED_SUCCESS, tokens
    )
    return (OutcomeStatus.VERIFIED_SUCCESS if ok else OutcomeStatus.FAILURE), tokens


# ---- statistics ---------------------------------------------------------------------------------
def clopper_pearson_upper(k: int, n: int, confidence: float = 0.95) -> float:
    """Exact one-sided upper bound of a binomial proportion."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    lo, hi = k / n, 1.0
    alpha = 1.0 - confidence
    for _ in range(60):
        mid = (lo + hi) / 2
        cdf = sum(math.comb(n, i) * mid**i * (1 - mid) ** (n - i) for i in range(k + 1))
        if cdf > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def quantiles(xs: Sequence[float]) -> tuple[float, float, float] | None:
    if not xs:
        return None
    s = sorted(xs)

    def q(p: float) -> float:
        return s[min(len(s) - 1, max(0, round(p * (len(s) - 1))))]

    return q(0.25), statistics.median(s), q(0.75)


def paired_diff_ci(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    return bootstrap_mean_ci([x - y for x, y in zip(xs, ys, strict=True)], resamples=2000)


def summarize(
    results: Sequence[StreamResult], scenario: Scenario, family: str | None = None
) -> dict[str, Any]:
    """Aggregate one (scenario, variant) over seeds."""
    n = len(results)
    fams = [f for r in results for f in r.families if family is None or f.family == family]
    truth_fams = [f for f in fams if f.onset is not None]
    fa_streams = sum(1 for r in results if any(f.false_alarms for f in r.families
                     if family is None or f.family == family))  # fmt: skip
    total_decisions = sum(r.n for r in results)
    out: dict[str, Any] = {
        "n_streams": n,
        "false_alarm_streams": fa_streams,
        "false_alarm_rate": fa_streams / n if n else 0.0,
        "false_alarm_ub95": clopper_pearson_upper(fa_streams, n),
        "false_alarms_per_1000": 1000.0
        * sum(f.false_alarms for f in fams)
        / max(1, total_decisions),
        "suspect_episodes_per_stream": sum(f.suspect_episodes for f in fams) / max(1, n),
        "quarantine_episodes_per_stream": sum(f.quarantine_episodes for f in fams) / max(1, n),
        "drift_streams": len(truth_fams),
        "detected": sum(1 for f in truth_fams if f.first_detection is not None),
        "missed": sum(1 for f in truth_fams if f.missed),
        "exposed": sum(1 for f in truth_fams if f.exposed),
        "delay_quartiles": quantiles([f.delay for f in truth_fams if f.delay is not None]),
        "recovered": sum(1 for f in truth_fams if f.recovered_at is not None),
        "recovery_delay_quartiles": quantiles(
            [f.recovery_delay for f in truth_fams if f.recovery_delay is not None]
        ),
        "quarantine_duration_quartiles": quantiles(
            [f.quarantine_duration for f in truth_fams if f.quarantine_duration is not None]
        ),
        "success_rate": sum(r.successes for r in results) / max(1, total_decisions),
        "tokens_per_success": sum(r.tokens for r in results)
        / max(1, sum(r.successes for r in results)),
        "probe_tokens_per_stream": sum(r.probe_tokens for r in results) / max(1, n),
        "probes_per_stream": sum(r.n_probe for r in results) / max(1, n),
        "fallback_decisions_per_stream": sum(r.n_fallback for r in results) / max(1, n),
        "alt_decisions_per_stream": sum(r.n_alt for r in results) / max(1, n),
        "first_enforced_median": statistics.median(
            [r.first_enforced for r in results if r.first_enforced is not None]
        ) if any(r.first_enforced is not None for r in results) else None,
        "prior_draws_mean": sum(r.prior_draws for r in results) / max(1, n),
        "cpu_s_per_observation": sum(r.cpu_s_per_observation for r in results) / max(1, n),
    }  # fmt: skip
    rate = [r.successes / r.n for r in results]
    out["success_rate_ci"] = bootstrap_mean_ci(rate, resamples=2000)
    return out


def result_to_json(r: StreamResult) -> dict[str, Any]:
    return asdict(r)
