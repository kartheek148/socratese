import math


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


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
