"""M6.1 unit tests: paired bound, hash stability, paired priors, evidence-driven policy."""

import math

import pytest

from experience_engine.domain.stats import newcombe_difference_bounds, newcombe_paired_bounds


# ---- paired statistics --------------------------------------------------------------------------
def _exact_coverage(n: int, probs: tuple[float, float, float, float], conf: float = 0.95) -> float:
    """Exact probability that the lower bound is <= the true difference (enumerates every table)."""
    p11, p10, p01, p00 = probs
    theta = p10 - p01
    lg = [math.lgamma(i + 1) for i in range(n + 2)]
    cov = 0.0
    for a in range(n + 1):
        for b in range(n + 1 - a):
            for c in range(n + 1 - a - b):
                d = n - a - b - c
                logp = lg[n] - lg[a] - lg[b] - lg[c] - lg[d]
                for k, p in ((a, p11), (b, p10), (c, p01), (d, p00)):
                    if k:
                        logp += k * math.log(p)
                if theta >= newcombe_paired_bounds(a, b, c, d, conf)[0]:
                    cov += math.exp(logp)
    return cov


@pytest.mark.parametrize(
    "probs",
    [
        (0.4, 0.1, 0.1, 0.4),
        (0.3, 0.2, 0.1, 0.4),
        (0.45, 0.05, 0.05, 0.45),
        (0.25, 0.25, 0.25, 0.25),
    ],
)
def test_paired_bound_has_near_nominal_exact_coverage(
    probs: tuple[float, float, float, float],
) -> None:
    # nominal 0.95 one-sided; the hybrid score interval is slightly liberal at isolated points
    assert _exact_coverage(40, probs) >= 0.94


def test_paired_bound_uses_pairing_and_is_tighter_than_the_independent_bound() -> None:
    # 240 tasks, 25% discordant, equal marginals: pairing removes the shared task noise
    both, only1, only2, neither = 90, 30, 30, 90
    lo_p, hi_p = newcombe_paired_bounds(both, only1, only2, neither, 0.95)
    n = both + only1 + only2 + neither
    lo_i, hi_i = newcombe_difference_bounds(both + only1, n, both + only2, n, 0.95)
    assert lo_p > lo_i and hi_p < hi_i
    wald = 1.645 * math.sqrt(only1 + only2) / n  # SE of a paired difference depends on b + c only
    assert lo_p == pytest.approx(-wald, abs=5e-3) and hi_p == pytest.approx(wald, abs=5e-3)
    indep = 1.645 * math.sqrt(2 * 0.25 / n)  # two independent samples of 240 at p = 0.5
    assert lo_i == pytest.approx(-indep, abs=5e-3)
    assert lo_p > 0.75 * lo_i  # pairing narrows the half-width by ~29% at 25% discordance


def test_paired_bound_edge_cases_and_symmetry() -> None:
    assert newcombe_paired_bounds(0, 0, 0, 0, 0.95) == (-1.0, 1.0)
    lo, hi = newcombe_paired_bounds(10, 0, 0, 10, 0.95)  # perfect agreement: still not exactly 0
    assert -0.2 < lo < 0 < hi < 0.2
    l1, u1 = newcombe_paired_bounds(40, 12, 3, 45, 0.95)
    l2, u2 = newcombe_paired_bounds(40, 3, 12, 45, 0.95)  # swapping the outcomes negates the bounds
    assert l1 == pytest.approx(-u2) and u1 == pytest.approx(-l2)
    assert newcombe_paired_bounds(0, 5, 0, 0, 0.95)[0] > 0  # always only outcome 1 wins
    lo_z, _ = newcombe_paired_bounds(0, 0, 0, 30, 0.95)  # a zero marginal does not divide by zero
    assert -0.2 < lo_z <= 0
