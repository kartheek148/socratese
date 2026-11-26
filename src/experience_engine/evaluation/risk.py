"""Task-level risk statistics for promotion (pure, deterministic, no dependencies).

Exact one-sided Clopper-Pearson bounds (real-valued counts are allowed so a task that regressed in
1 of 2 repetitions contributes 0.5), task-level regression evidence and a paired bootstrap bound.
The unit is the distinct task; repetitions are averaged inside it because they are correlated
draws of a stochastic model, not independent samples.
"""

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence

from experience_engine.domain.evaluation import RiskEvidence, Suite, TaskResult

SCORED = (Suite.SOURCE_REPLAY, Suite.HELD_OUT, Suite.TRANSFER)
COMPARED = (Suite.HELD_OUT, Suite.TRANSFER)
VERIFIED = "VERIFIED_SUCCESS"
UNKNOWN = "UNKNOWN"


def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if beta_cdf(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def cp_upper(x: float, n: float, confidence: float) -> float:
    """One-sided upper confidence bound on a proportion (exact, Clopper-Pearson)."""
    if n <= 0:
        return 1.0
    if x >= n:
        return 1.0
    return beta_ppf(confidence, x + 1.0, n - x)


def cp_lower(x: float, n: float, confidence: float) -> float:
    """One-sided lower confidence bound on a proportion (exact, Clopper-Pearson)."""
    if n <= 0 or x <= 0:
        return 0.0
    return beta_ppf(1.0 - confidence, x, n - x + 1.0)


def sign_test_worse(only_baseline: int, only_treatment: int) -> float:
    """Exact one-sided p-value that the treatment is worse: P(X >= only_baseline | n, 0.5)."""
    n = only_baseline + only_treatment
    if n == 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(only_baseline, n + 1)) / float(2**n)


def paired_bootstrap_bounds(
    diffs: Sequence[float], confidence: float, *, resamples: int, seed: int
) -> tuple[float, float, float]:
    """(mean, one-sided lower bound, one-sided upper bound) of the mean paired difference."""
    n = len(diffs)
    if n == 0:
        return (0.0, -1.0, 1.0)
    mean = sum(diffs) / n
    if n == 1 or all(v == diffs[0] for v in diffs):
        return (mean, mean, mean)
    rng = random.Random(seed)
    idx = range(n)
    means = sorted(sum(diffs[rng.choice(idx)] for _ in idx) / n for _ in range(resamples))
    tail = 1.0 - confidence
    lo = means[int(tail * resamples)]
    hi = means[min(resamples - 1, int(confidence * resamples))]
    return (mean, lo, hi)


def build_risk_evidence(rows: Iterable[TaskResult], treatment_arm: str) -> RiskEvidence:
    """Pair the treatment with the baseline run of the same (suite, task, repetition)."""
    base: dict[tuple[str, str, int], TaskResult] = {}
    treat: dict[tuple[str, str, int], TaskResult] = {}
    terminations: dict[str, int] = defaultdict(int)
    for r in rows:
        key = (r.suite.value, r.task_id, r.seed)
        if r.arm.value == "A_BASELINE":
            base[key] = r
        elif r.arm.value == treatment_arm:
            treat[key] = r
            if r.termination:
                terminations[r.termination] += 1

    solved: dict[tuple[str, str], list[bool]] = defaultdict(list)  # (suite, task) -> regressed?
    rates: dict[tuple[str, str], list[tuple[bool, bool]]] = defaultdict(list)
    variants: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    dropped = 0
    for key, t in treat.items():
        b = base.get(key)
        suite = Suite(key[0])
        if b is None or suite not in SCORED:
            continue
        if b.status == UNKNOWN or t.status == UNKNOWN:
            dropped += 1
            continue
        bv, tv = b.status == VERIFIED, t.status == VERIFIED
        rates[(key[0], key[1])].append((bv, tv))
        if bv:
            solved[(key[0], key[1])].append(not tv)
            v = variants[t.variant]
            v[0] += 1
            v[1] += int(not tv)

    n_pairs = sum(len(v) for v in solved.values())
    n_reg = sum(sum(v) for v in solved.values())
    mass = sum(sum(v) / len(v) for v in solved.values())
    diffs: list[float] = []
    improved = 0
    source_mass, source_tasks = 0.0, 0
    for (suite_name, _task), pairs in sorted(rates.items()):
        t_rate = sum(tv for _, tv in pairs) / len(pairs)
        b_rate = sum(bv for bv, _ in pairs) / len(pairs)
        if Suite(suite_name) in COMPARED:
            diffs.append(t_rate - b_rate)
            improved += int(t_rate > b_rate)
        else:
            source_tasks += 1
            source_mass += t_rate
    return RiskEvidence(
        n_pairs=n_pairs,
        n_regressions=n_reg,
        n_dropped_unknown=dropped,
        regression_tasks=len(solved),
        regression_mass=mass,
        improvement_tasks=improved,
        variant_pairs={k: list(v) for k, v in sorted(variants.items())},
        paired_diffs=diffs,
        source_tasks=source_tasks,
        source_mass=source_mass,
        termination_counts=dict(sorted(terminations.items())),
    )
