"""Prequential calibration of the success and cost estimates.

Records are processed in log order; each prediction is made from the statistics accumulated
*before* the record's outcome is observed, so the check is honest about what the controller
would have believed at decision time. Only predictions from cells with at least `warmup`
earlier observations are scored (earlier ones are the uninformative prior).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.domain.controller import CalibrationSummary, DecisionOutcomeRecord, cell_key


@dataclass(frozen=True)
class CalibrationGate:
    min_n: int = 100
    max_ece: float = 0.10
    # Brier skill versus the global rate is reported but not gated by default: when actions do not
    # differ, per-cell estimates have slightly *negative* skill, and gating on it would switch
    # exploration off exactly when more evidence is needed.
    min_skill: float | None = None
    warmup: int = 5
    bins: int = 10


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    mean_obs: float


def reliability_table(
    preds: Sequence[float], obs: Sequence[bool], bins: int = 10
) -> list[ReliabilityBin]:
    out: list[ReliabilityBin] = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(preds) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if idx:
            out.append(
                ReliabilityBin(
                    lo,
                    hi,
                    len(idx),
                    sum(preds[i] for i in idx) / len(idx),
                    sum(obs[i] for i in idx) / len(idx),
                )
            )
    return out


def expected_calibration_error(
    preds: Sequence[float], obs: Sequence[bool], bins: int = 10
) -> float:
    n = len(preds)
    if n == 0:
        return 0.0
    return sum(b.n / n * abs(b.mean_pred - b.mean_obs) for b in reliability_table(preds, obs, bins))


def brier(preds: Sequence[float], obs: Sequence[bool]) -> float:
    return sum((p - float(o)) ** 2 for p, o in zip(preds, obs, strict=True)) / len(preds)


def prequential_calibration(
    known: Sequence[DecisionOutcomeRecord],
    cost_prior_weight: float = 5.0,
    gate: CalibrationGate | None = None,
) -> CalibrationSummary:
    gate = gate or CalibrationGate()
    cells: dict[str, list[int]] = {}
    ctx: dict[str, list[int]] = {}  # (family|risk) pooled token statistics
    glob = [0, 0]  # n, successes
    preds: list[float] = []
    base: list[float] = []
    obs: list[bool] = []
    cost_err: list[float] = []
    cost_ape: list[float] = []
    cost_actual: list[float] = []
    for r in known:
        ck = cell_key(r.family, r.risk_level, r.action)
        c = cells.setdefault(ck, [0, 0, 0])
        pk = f"{r.family or '-'}|{r.risk_level}"
        pool = ctx.setdefault(pk, [0, 0])
        if c[0] >= gate.warmup:
            preds.append((c[1] + 1) / (c[0] + 2))
            base.append((glob[1] + 1) / (glob[0] + 2))
            obs.append(r.success)
            prior = pool[1] / pool[0] if pool[0] else 0.0
            pred_cost = (c[2] + cost_prior_weight * prior) / (c[0] + cost_prior_weight)
            cost_err.append(pred_cost - r.tokens)
            cost_actual.append(float(r.tokens))
            if r.tokens > 0:
                cost_ape.append(abs(pred_cost - r.tokens) / r.tokens)
        c[0] += 1
        c[1] += int(r.success)
        c[2] += r.tokens
        pool[0] += 1
        pool[1] += r.tokens
        glob[0] += 1
        glob[1] += int(r.success)
    n = len(preds)
    if n == 0:
        return CalibrationSummary(
            n=0,
            brier=None,
            brier_skill=None,
            ece=None,
            cost_bias=None,
            cost_mape=None,
            ok=False,
            reasons=["no scored predictions (insufficient history per cell)"],
        )
    b = brier(preds, obs)
    b0 = brier(base, obs)
    skill = 0.0 if b0 == 0 else 1.0 - b / b0
    ece = expected_calibration_error(preds, obs, gate.bins)
    mean_actual = sum(cost_actual) / len(cost_actual) if cost_actual else 0.0
    bias = (sum(cost_err) / len(cost_err)) / mean_actual if mean_actual > 0 else None
    mape = sum(cost_ape) / len(cost_ape) if cost_ape else None
    reasons: list[str] = []
    if n < gate.min_n:
        reasons.append(f"only {n} scored predictions (< {gate.min_n})")
    if ece > gate.max_ece:
        reasons.append(f"ECE {ece:.3f} > {gate.max_ece}")
    if gate.min_skill is not None and skill < gate.min_skill:
        reasons.append(f"Brier skill {skill:.3f} < {gate.min_skill} versus the global rate")
    return CalibrationSummary(
        n=n,
        brier=round(b, 6),
        brier_skill=round(skill, 6),
        ece=round(ece, 6),
        cost_bias=None if bias is None else round(bias, 6),
        cost_mape=None if mape is None else round(mape, 6),
        ok=not reasons,
        reasons=reasons,
    )
