import math
from functools import lru_cache


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@lru_cache(maxsize=64)
def normal_quantile(confidence: float) -> float:
    """One-sided standard-normal quantile by bisection on the erf-based CDF (no scipy)."""
    lo, hi = 0.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < confidence:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def wilson_one_sided(successes: int, n: int, confidence: float) -> tuple[float, float]:
    """One-sided Wilson score bounds (lower, upper), each at `confidence`. n == 0 -> (0, 1)."""
    if n <= 0:
        return (0.0, 1.0)
    z = normal_quantile(confidence)
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom))


def newcombe_difference_bounds(
    s1: int, n1: int, s2: int, n2: int, confidence: float
) -> tuple[float, float]:
    """Newcombe-Wilson hybrid score bounds of p1 - p2 (independent samples), one-sided each.

    Returns (lower, upper). With an empty sample the interval is the vacuous (-1, 1).
    """
    if n1 <= 0 or n2 <= 0:
        return (-1.0, 1.0)
    p1, p2 = s1 / n1, s2 / n2
    l1, u1 = wilson_one_sided(s1, n1, confidence)
    l2, u2 = wilson_one_sided(s2, n2, confidence)
    d = p1 - p2
    lower = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


def newcombe_paired_bounds(
    both: int, only1: int, only2: int, neither: int, confidence: float
) -> tuple[float, float]:
    """Newcombe (1998) hybrid-score bounds of p1 - p2 for PAIRED binary outcomes, one-sided each.

    `only1` = tasks where outcome 1 succeeded and outcome 2 failed, `only2` the reverse. Unlike
    `newcombe_difference_bounds` this uses the within-task correlation (phi, shrunk toward 0 by
    the continuity correction n/2), so concordant tasks do not inflate the interval. Returns
    (lower, upper); with no pairs it is the vacuous (-1, 1).
    """
    n = both + only1 + only2 + neither
    if n <= 0:
        return (-1.0, 1.0)
    p1, p2 = (both + only1) / n, (both + only2) / n
    l1, u1 = wilson_one_sided(both + only1, n, confidence)
    l2, u2 = wilson_one_sided(both + only2, n, confidence)
    margins = (both + only1) * (only2 + neither) * (both + only2) * (only1 + neither)
    phi = 0.0
    if margins > 0:
        cross = both * neither - only1 * only2
        shrunk = max(0.0, cross - n / 2) if cross > 0 else min(0.0, cross + n / 2)
        phi = shrunk / math.sqrt(margins)
    d = p1 - p2
    lower = d - math.sqrt(
        max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2)
    )
    upper = d + math.sqrt(
        max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2)
    )
    return (max(-1.0, lower), min(1.0, upper))
