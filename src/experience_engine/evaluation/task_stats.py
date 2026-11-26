"""Statistics for task-clustered, repeated stochastic observations (pure, deterministic).

Principles (M5.10 design, section 4):
* the distinct task is the unit; repetitions of a task are correlated draws and are collapsed to a
  task-level outcome first, never counted as independent samples;
* exact (Clopper-Pearson) intervals are applied ONLY to integer Bernoulli counts: the functions
  here refuse fractional counts instead of interpolating;
* paired differences and ratios use a cluster bootstrap that resamples whole tasks;
* when the evidence is too thin the caller gets an evidence REQUIREMENT, never a weaker threshold.
"""

import math
import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from experience_engine.evaluation.risk import cp_lower, cp_upper

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260101


def task_binary(outcomes: Sequence[bool | None]) -> bool | None:
    """Task-level outcome from its repetitions: strict majority of KNOWN repetitions.
    Exactly half -> None (indeterminate: dropped and counted, never evidence).
    All unknown -> None."""
    known = [o for o in outcomes if o is not None]
    if not known:
        return None
    ok = sum(known)
    if 2 * ok == len(known):
        return None
    return 2 * ok > len(known)


def _require_integer(x: float, name: str) -> int:
    if abs(x - round(x)) > 1e-9:
        raise ValueError(
            f"{name}={x} is not an integer: exact binomial bounds need integer Bernoulli counts "
            "(collapse repetitions to task-level outcomes first)"
        )
    return int(round(x))


def binom_upper(events: float, n: float, confidence: float) -> float:
    return cp_upper(_require_integer(events, "events"), _require_integer(n, "n"), confidence)


def binom_lower(events: float, n: float, confidence: float) -> float:
    return cp_lower(_require_integer(events, "events"), _require_integer(n, "n"), confidence)


def wilson_lower(successes: int, n: int, confidence: float = 0.95) -> float:
    """One-sided Wilson lower bound (integer counts)."""
    if n == 0:
        return 0.0
    z = _z(confidence)
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - half) / denom)


def _z(confidence: float) -> float:
    # one-sided normal quantile by bisection on the erf-based CDF (no scipy dependency)
    lo, hi = 0.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < confidence:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def required_tasks(
    delta: float, confidence: float, events: int = 0, limit: int = 2000
) -> int | None:
    """Smallest n (>= events) such that the exact upper bound for `events` regressions among n
    baseline-solved tasks is <= delta; None if unreachable within `limit`."""
    n = max(1, events)
    while n <= limit:
        if cp_upper(events, n, confidence) <= delta:
            return n
        n += 1
    return None


@dataclass(frozen=True)
class BinomialEvidence:
    events: int
    n: int
    ucb: float
    lcb: float
    needed_total: int | None  # tasks needed at the observed event count (optimistic)
    additional_needed: int | None


def binomial_evidence(events: int, n: int, delta: float, confidence: float) -> BinomialEvidence:
    need = required_tasks(delta, confidence, events)
    return BinomialEvidence(
        events=events,
        n=n,
        ucb=binom_upper(events, n, confidence),
        lcb=binom_lower(events, n, confidence),
        needed_total=need,
        additional_needed=None if need is None else max(0, need - n),
    )


def cluster_bootstrap(
    units: Sequence[object],
    statistic: Callable[[Sequence[object]], float | None],
    confidence: float = 0.95,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None, float | None, int]:
    """(point, one-sided lower bound, one-sided upper bound, undefined resamples).

    `units` are whole tasks (any per-task record); each resample draws tasks with replacement and
    recomputes `statistic`. Resamples where the statistic is undefined (None) are skipped and
    counted; if more than 5% are undefined the bounds are withheld (None).
    """
    point = statistic(units)
    n = len(units)
    if n == 0 or point is None:
        return point, None, None, 0
    rng = random.Random(seed)
    vals: list[float] = []
    undefined = 0
    for _ in range(resamples):
        v = statistic([units[rng.randrange(n)] for _ in range(n)])
        if v is None:
            undefined += 1
        else:
            vals.append(v)
    if undefined > 0.05 * resamples or not vals:
        return point, None, None, undefined
    vals.sort()
    tail = 1.0 - confidence
    lo = vals[int(tail * len(vals))]
    hi = vals[min(len(vals) - 1, int(confidence * len(vals)))]
    return point, lo, hi, undefined


def paired_difference(
    diffs: Sequence[float], confidence: float = 0.95, **kw: int
) -> tuple[float | None, float | None, float | None, int]:
    def mean(sample: Sequence[object]) -> float | None:
        return sum(sample) / len(sample) if sample else None  # type: ignore[arg-type]

    return cluster_bootstrap(list(diffs), mean, confidence, **kw)


@dataclass(frozen=True)
class CostUnit:
    """Per task: total tokens and verified successes of the strategy and of the baseline."""

    tokens_strategy: float
    verified_strategy: float
    tokens_baseline: float
    verified_baseline: float


def cost_per_success_ratio(
    units: Sequence[CostUnit], confidence: float = 0.95, **kw: int
) -> tuple[float | None, float | None, float | None, int]:
    """(strategy tokens per verified success) / (baseline's), cluster bootstrap over tasks.
    Failures' cost stays in the numerator (it is paid either way)."""

    def ratio(sample: Sequence[object]) -> float | None:
        s: Sequence[CostUnit] = sample  # type: ignore[assignment]
        vs, vb = sum(u.verified_strategy for u in s), sum(u.verified_baseline for u in s)
        if vs <= 0 or vb <= 0:
            return None
        return (sum(u.tokens_strategy for u in s) / vs) / (sum(u.tokens_baseline for u in s) / vb)

    return cluster_bootstrap(list(units), ratio, confidence, **kw)


@dataclass(frozen=True)
class StratifiedEstimate:
    overall: float
    lo: float | None
    hi: float | None
    by_stratum: dict[str, tuple[int, float]]  # name -> (tasks, mean)
    weights: dict[str, float]


def stratified_mean(
    strata: Mapping[str, Sequence[float]],
    weights: Mapping[str, float] | None = None,
    confidence: float = 0.95,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> StratifiedEstimate:
    """Weighted overall mean of per-task values with stratum-level means. Default weights are the
    stratum task counts (the sampled population shares); resampling happens within strata."""
    names = sorted(k for k, v in strata.items() if v)
    counts = {k: len(strata[k]) for k in names}
    total = sum(counts.values())
    w = dict(weights) if weights else {k: counts[k] / total for k in names}
    norm = sum(w[k] for k in names)
    w = {k: w[k] / norm for k in names}

    def overall(sample: Mapping[str, Sequence[float]]) -> float:
        return sum(w[k] * (sum(sample[k]) / len(sample[k])) for k in names)

    point = overall({k: strata[k] for k in names})
    rng = random.Random(seed)
    boots = []
    for _ in range(resamples):
        boots.append(
            overall(
                {k: [strata[k][rng.randrange(counts[k])] for _ in range(counts[k])] for k in names}
            )
        )
    boots.sort()
    lo = boots[int((1 - confidence) * resamples)]
    hi = boots[min(resamples - 1, int(confidence * resamples))]
    return StratifiedEstimate(
        point, lo, hi, {k: (counts[k], sum(strata[k]) / counts[k]) for k in names}, w
    )


def collapse_by_task(
    rows: Sequence[tuple[str, bool | None]],
) -> tuple[dict[str, bool], int]:
    """Per-repetition (task_id, outcome) -> task-level outcomes and the indeterminate count."""
    acc: dict[str, list[bool | None]] = defaultdict(list)
    for tid, o in rows:
        acc[tid].append(o)
    out: dict[str, bool] = {}
    indeterminate = 0
    for tid, outs in acc.items():
        b = task_binary(outs)
        if b is None:
            indeterminate += 1
        else:
            out[tid] = b
    return out, indeterminate
