"""Paired, task-level statistics for comparing agent configurations (pure, deterministic).

The unit of analysis is the distinct task. Repetitions of a task are repeated draws from a
non-deterministic model, so they are averaged *within* the task first and never counted as
independent samples. UNKNOWN outcomes (e.g. provider outages) are missing data, not failures.
"""

import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from experience_engine.domain.experiment import RunRow

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260101
SOLVED_THRESHOLD = 0.5  # a task is "solved" if it was verified in >= half of its repetitions


def task_rates(rows: Iterable[RunRow], arm: str) -> dict[str, float]:
    """Mean verified-rate per task over its known (non-UNKNOWN) repetitions."""
    acc: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r.arm == arm and r.status != "UNKNOWN":
            acc[r.task_id].append(r.verified)
    return {t: sum(v) / len(v) for t, v in acc.items()}


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """(mean, lo, hi): percentile bootstrap over tasks. Fixed RNG seed -> reproducible."""
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mean = sum(values) / n
    if n == 1 or all(v == values[0] for v in values):
        return (mean, mean, mean)
    rng = random.Random(seed)
    idx = range(n)
    means = sorted(sum(values[rng.choice(idx)] for _ in idx) / n for _ in range(resamples))
    lo = means[int((alpha / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (mean, lo, hi)


def mcnemar_exact(only_x: int, only_y: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    n = only_x + only_y
    if n == 0:
        return 1.0
    k = min(only_x, only_y)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / float(2**n)
    return min(1.0, 2 * tail)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values (family-wise error control)."""
    order = sorted(pvalues, key=lambda k: pvalues[k])
    m = len(order)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, key in enumerate(order):
        running = max(running, min(1.0, (m - i) * pvalues[key]))
        adjusted[key] = running
    return adjusted


@dataclass(frozen=True)
class PairedComparison:
    x: str
    y: str
    n_tasks: int  # distinct tasks with known outcomes in both arms
    n_excluded: int  # tasks dropped because an arm had no known repetition
    rate_x: float
    rate_y: float
    diff: float  # mean per-task (x - y)
    ci_lo: float
    ci_hi: float
    only_x: int  # discordant: solved by x, not by y
    only_y: int
    both: int
    neither: int
    p_mcnemar: float

    @property
    def ci_excludes_zero(self) -> bool:
        return self.ci_lo > 0 or self.ci_hi < 0


def compare(
    rows: Sequence[RunRow],
    x: str,
    y: str,
    *,
    where: Callable[[RunRow], bool] | None = None,
    threshold: float = SOLVED_THRESHOLD,
) -> PairedComparison:
    subset = [r for r in rows if where is None or where(r)]
    rx, ry = task_rates(subset, x), task_rates(subset, y)
    universe = {r.task_id for r in subset if r.arm in (x, y)}
    common = sorted(rx.keys() & ry.keys())
    diffs = [rx[t] - ry[t] for t in common]
    mean, lo, hi = bootstrap_mean_ci(diffs)
    sx = {t: rx[t] >= threshold for t in common}
    sy = {t: ry[t] >= threshold for t in common}
    only_x = sum(sx[t] and not sy[t] for t in common)
    only_y = sum(sy[t] and not sx[t] for t in common)
    both = sum(sx[t] and sy[t] for t in common)
    n = len(common)
    return PairedComparison(
        x=x,
        y=y,
        n_tasks=n,
        n_excluded=len(universe) - n,
        rate_x=sum(rx[t] for t in common) / n if n else 0.0,
        rate_y=sum(ry[t] for t in common) / n if n else 0.0,
        diff=mean,
        ci_lo=lo,
        ci_hi=hi,
        only_x=only_x,
        only_y=only_y,
        both=both,
        neither=n - both - only_x - only_y,
        p_mcnemar=mcnemar_exact(only_x, only_y),
    )


def success_at_budget(rows: Sequence[RunRow], arm: str, cap: int) -> float:
    """Per-task rate of runs that were verified *within* `cap` total tokens, averaged over tasks."""
    acc: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        if r.arm == arm and r.status != "UNKNOWN":
            acc[r.task_id].append(r.verified and r.tokens <= cap)
    if not acc:
        return 0.0
    return sum(sum(v) / len(v) for v in acc.values()) / len(acc)


def budget_curve(rows: Sequence[RunRow], arm: str, caps: Sequence[int]) -> dict[int, float]:
    return {c: success_at_budget(rows, arm, c) for c in caps}
