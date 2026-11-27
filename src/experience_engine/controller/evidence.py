"""M6.1 decision rules: paired validation priors, live evidence, drift guard, exploration.

Pure functions of the stored `DecisionFeatures` and the policy parameters (replayable). Used only
when `features.evidence_module` is set; the M6 rules in `policy.py` are untouched otherwise.

Per alternative `k` of the rule default `d` there are two evidence tiers, chosen by data
availability alone (never by outcome):
  P  a paired validation cell with >= n_min_pairs tasks: Newcombe paired bounds of p_k - p_d.
     Live (unpaired) data cannot tighten a paired bound, so tier P never explores.
  L  no usable paired cell: the M6 rule (live cells, n_min each, independent Newcombe bound).
Reliability (the bound) is a hard constraint; cost is only optimised among those that meet it.
"""

from dataclasses import dataclass

from experience_engine.controller.estimator import cost_hat, p_hat
from experience_engine.controller.modes import MODE_DEFERRED, MODE_GREEDY
from experience_engine.controller.paired import required_pairs
from experience_engine.domain.controller import (
    ControllerKind,
    ControllerParams,
    DecisionFeatures,
    EvidenceView,
    PairedCell,
    PairedView,
)
from experience_engine.domain.stats import (
    newcombe_difference_bounds,
    newcombe_paired_bounds,
    wilson_one_sided,
)

TIER_PAIRED = "P"
TIER_LIVE = "L"
TIER_NONE = "-"


@dataclass(frozen=True)
class Assessment:
    key: str
    tier: str
    lb: float  # lower bound of theta = p_alt - p_default
    ub: float
    n: int  # paired tasks (tier P) or live trials of the alternative (tier L / none)
    ratio: float | None  # tokens per verified success: alternative / default
    drift: str | None = None  # why a prior-backed alternative was invalidated
    req_pairs: int | None = None  # paired tasks needed to resolve (tier P, unresolved)


def _context_tokens(ev: dict[str, EvidenceView]) -> float:
    total = sum(e.n for e in ev.values())
    return sum(e.token_sum for e in ev.values()) / total if total else 0.0


def _drift(
    ev: dict[str, EvidenceView],
    p: PairedView,
    default_key: str,
    key: str,
    delta: float,
    min_n: int,
    confidence: float,
) -> str | None:
    """Live outcomes that contradict the prior badly enough to distrust it (coarse by design)."""
    alt_prior = (p.both + p.alt_only) / p.n
    def_prior = (p.both + p.default_only) / p.n
    ek, ed = ev.get(key), ev.get(default_key)
    if ek is not None and ek.n >= min_n:
        _, ub = wilson_one_sided(ek.successes, ek.n, confidence)
        if ub < alt_prior - delta:
            return f"alternative live success (n={ek.n}) is far below its validation rate"
    if ed is not None and ed.n >= min_n:
        lb, _ = wilson_one_sided(ed.successes, ed.n, confidence)
        if lb > def_prior + delta:
            return f"default live success (n={ed.n}) is far above its validation rate"
    return None


def assess_alternatives(f: DecisionFeatures, params: ControllerParams) -> dict[str, Assessment]:
    ep = params.evidence
    assert ep is not None
    delta = params.delta[f.risk_level]
    conf = params.confidence
    ev = {e.key: e for e in f.evidence}
    pv = {p.key: p for p in f.paired}
    d = f.default_key
    ctx_tokens = _context_tokens(ev)
    out: dict[str, Assessment] = {}
    for a in f.actions:
        k = a.key
        if k == d:
            continue
        p = pv.get(k)
        ek, ed = ev.get(k), ev.get(d)
        if p is not None and p.n >= ep.n_min_pairs:
            lb, ub = newcombe_paired_bounds(p.both, p.alt_only, p.default_only, p.neither, conf)
            cps_alt = (p.alt_tokens / p.n) / p_hat(p.both + p.alt_only, p.n)
            cps_def = (p.default_tokens / p.n) / p_hat(p.both + p.default_only, p.n)
            req = None
            if lb < -delta:
                req = required_pairs(PairedCell(**p.model_dump(exclude={"key"})), delta, conf)
            drift = _drift(ev, p, d, k, delta, ep.drift_min_n, conf)
            out[k] = Assessment(k, TIER_PAIRED, lb, ub, p.n, cps_alt / cps_def, drift, req)
        elif ek is not None and ed is not None and ek.n >= params.n_min and ed.n >= params.n_min:
            lb, ub = newcombe_difference_bounds(ek.successes, ek.n, ed.successes, ed.n, conf)
            cps_k = cost_hat(ek, ctx_tokens, params.cost_prior_weight) / p_hat(ek.successes, ek.n)
            cps_d = cost_hat(ed, ctx_tokens, params.cost_prior_weight) / p_hat(ed.successes, ed.n)
            out[k] = Assessment(k, TIER_LIVE, lb, ub, ek.n, cps_k / cps_d)
        else:
            out[k] = Assessment(k, TIER_NONE, -1.0, 1.0, ek.n if ek else 0, None)
    return out


def _log(est: dict[str, float], assess: dict[str, Assessment]) -> dict[str, float]:
    out = dict(est)
    for k, a in assess.items():
        out[f"lb:{k}"] = round(a.lb, 6)
        out[f"ub:{k}"] = round(a.ub, 6)
        out[f"tier_paired:{k}"] = 1.0 if a.tier == TIER_PAIRED else 0.0
        out[f"n_evidence:{k}"] = float(a.n)
        if a.ratio is not None:
            out[f"cps_ratio:{k}"] = round(a.ratio, 6)
        if a.req_pairs is not None:
            out[f"req_pairs:{k}"] = float(a.req_pairs)
        if a.drift is not None:
            out[f"drift:{k}"] = 1.0
    return out


def greedy_evidence(
    f: DecisionFeatures, params: ControllerParams, base_estimates: dict[str, float]
) -> tuple[str, str, dict[str, float], str, dict[str, Assessment]]:
    """(key, reason, estimates, mode, assessments). Never leaves the feasible set; keeps the rule
    default on any doubt."""
    d = f.default_key
    delta = params.delta[f.risk_level]
    assess = assess_alternatives(f, params)
    est = _log(base_estimates, assess)
    if not any(a.tier != TIER_NONE for a in assess.values()):
        n_d = next((e.n for e in f.evidence if e.key == d), 0)
        why = f"no usable evidence for any alternative (default live n={n_d}, no paired prior)"
        return d, f"[{MODE_DEFERRED}] {why}; rule baseline {d}.", est, MODE_DEFERRED, assess
    admissible = [
        k
        for k, a in assess.items()
        if a.tier != TIER_NONE and a.lb >= -delta and a.drift is None and a.ratio is not None
    ]
    if admissible:
        best = min(admissible, key=lambda k: (assess[k].ratio or 0.0, k))
        a = assess[best]
        if a.ratio is not None and a.ratio <= 1.0 - params.min_cost_saving:
            src = f"paired n={a.n}" if a.tier == TIER_PAIRED else f"live n={a.n}"
            return (
                best,
                f"[{MODE_GREEDY}] {best} is non-inferior to {d} ({src}, lower bound "
                f"{a.lb:+.3f} >= -{delta}) and cheaper "
                f"({a.ratio:.2f}x tokens per verified success).",
                est,
                MODE_GREEDY,
                assess,
            )
    notes = []
    for k, a in sorted(assess.items()):
        if a.drift is not None:
            notes.append(f"{k}: prior invalidated ({a.drift})")
        elif a.tier == TIER_NONE:
            notes.append(f"{k}: no evidence")
        elif a.lb >= -delta:
            notes.append(f"{k}: non-inferior but saves < {params.min_cost_saving:.0%}")
        elif a.ub < -delta:
            notes.append(f"{k}: confidently worse")
        elif a.req_pairs is not None:
            notes.append(f"{k}: unresolved, needs about {a.req_pairs} paired tasks")
        else:
            notes.append(f"{k}: unresolved")
    return (
        d,
        f"[{MODE_GREEDY}] keep {d}: " + "; ".join(notes) + ".",
        est,
        MODE_GREEDY,
        assess,
    )


def explore_target(
    p_k: float, ed: EvidenceView, delta: float, confidence: float, n_min: int, cap: int
) -> int | None:
    """Smallest number of trials of the alternative for which the independent lower bound would
    reach -delta if the current point estimates held; None if it cannot within `cap`."""
    for m in range(n_min, cap + 1):
        lo, _ = newcombe_difference_bounds(round(p_k * m), m, ed.successes, ed.n, confidence)
        if lo >= -delta:
            return m
    return None


def explorable_evidence(
    f: DecisionFeatures,
    params: ControllerParams,
    greedy: str,
    assess: dict[str, Assessment],
) -> list[str]:
    """Actions exploration may draw from: the greedy action plus alternatives whose
    non-inferiority is still unresolved, that could plausibly save enough, and that have not
    reached their sample target. Empty (deterministic) when nothing qualifies."""
    ep = params.evidence
    if (
        ep is None
        or f.controller_kind != ControllerKind.EVIDENCE_DRIVEN
        or params.epsilon <= 0.0
        or f.u is None
        or f.risk_level not in params.exploration_risk_levels
        or f.task_family not in params.exploration_families
        or not f.calibration_ok
        or f.n_verified_total < params.min_total_verified
    ):
        return []
    ev = {e.key: e for e in f.evidence}
    d = f.default_key
    ed = ev.get(d)
    if ed is None or ed.n < params.n_min:
        return []
    delta = params.delta[f.risk_level]
    conf = params.confidence
    ctx_tokens = _context_tokens(ev)
    cps_d = cost_hat(ed, ctx_tokens, params.cost_prior_weight) / p_hat(ed.successes, ed.n)
    pool = {greedy}
    for k, a in assess.items():
        if a.tier == TIER_PAIRED or a.lb >= -delta or a.ub < -delta:
            continue  # paired evidence cannot be improved online; resolved either way
        ek = ev.get(k)
        n_k = ek.n if ek is not None else 0
        ub_p = wilson_one_sided(ek.successes, ek.n, conf)[1] if ek is not None and ek.n else 1.0
        optimistic = cost_hat(ek, ctx_tokens, params.cost_prior_weight) / min(1.0, max(ub_p, 1e-9))
        if optimistic > (1.0 - params.min_cost_saving) * cps_d:
            continue  # even the optimistic estimate cannot save enough: hopeless
        p_k = p_hat(ek.successes, ek.n) if ek is not None and ek.n else p_hat(ed.successes, ed.n)
        target = explore_target(p_k, ed, delta, conf, params.n_min, ep.explore_target_cap)
        if target is None or n_k >= target:
            continue
        pool.add(k)
    return sorted(pool)
