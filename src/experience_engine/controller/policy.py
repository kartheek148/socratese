"""Cost-aware and bandit controller policies.

`choose` is a pure function of the stored `DecisionFeatures` and the policy parameters, so any
decision can be recomputed later (see `replay.py`). Constraints sit outside the cost objective:
protected risk classes return the rule baseline unchanged; a deviation needs enough verified
evidence and a non-inferiority bound; cost is only minimised among actions that satisfy them.
"""

from dataclasses import dataclass

from experience_engine.controller.estimator import cost_hat, p_hat
from experience_engine.controller.features import build_features
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
    DecisionFeatures,
    EvidenceView,
    parse_action_key,
)
from experience_engine.domain.decision import ControllerDecision, Strategy
from experience_engine.domain.stats import newcombe_difference_bounds, wilson_one_sided
from experience_engine.execution.controller import SELECTED_CAPABILITY_KEY, ControllerInput

MODE_DEFERRED = "DEFERRED"
MODE_GREEDY = "GREEDY"
MODE_EXPLORE = "EXPLORE"


@dataclass(frozen=True)
class Choice:
    selected_key: str
    probability: float
    propensities: dict[str, float]
    estimates: dict[str, float]
    reason: str
    mode: str


def _deferred(f: DecisionFeatures, why: str, estimates: dict[str, float] | None = None) -> Choice:
    return Choice(
        f.default_key,
        1.0,
        {f.default_key: 1.0},
        estimates or {},
        f"[{MODE_DEFERRED}] {why}; rule baseline {f.default_key}.",
        MODE_DEFERRED,
    )


def _estimates(f: DecisionFeatures, params: ControllerParams) -> dict[str, float]:
    ev = {e.key: e for e in f.evidence}
    total_n = sum(e.n for e in ev.values())
    prior = sum(e.token_sum for e in ev.values()) / total_n if total_n else 0.0
    out: dict[str, float] = {}
    for a in f.actions:
        e = ev.get(a.key)
        if e is not None:
            out[f"n:{a.key}"] = float(e.n)
            out[f"p_hat:{a.key}"] = round(p_hat(e.successes, e.n), 6)
            c = cost_hat(e, prior, params.cost_prior_weight)
            out[f"cost_hat:{a.key}"] = round(c, 3)
            out[f"cps_hat:{a.key}"] = round(c / p_hat(e.successes, e.n), 3)
    return out


def _greedy(
    f: DecisionFeatures, params: ControllerParams
) -> tuple[str, str, dict[str, float], str]:
    """Returns (key, reason, estimates, mode). Never leaves the feasible set; defaults on doubt."""
    ev = {e.key: e for e in f.evidence}
    d = f.default_key
    est = _estimates(f, params)
    enough = [a.key for a in f.actions if a.key in ev and ev[a.key].n >= params.n_min]
    if d not in enough:
        n_d = ev[d].n if d in ev else 0
        why = f"insufficient evidence for the default (n={n_d} < {params.n_min})"
        return d, f"[{MODE_DEFERRED}] {why}; rule baseline {d}.", est, MODE_DEFERRED
    risk = f.risk_level
    delta = params.delta[risk]
    conf = params.confidence
    tau = params.required_success.get(risk, 0.0)
    lower = {k: wilson_one_sided(ev[k].successes, ev[k].n, conf)[0] for k in enough}
    # objective: expected tokens per verified success (failures' cost is paid either way)
    cost = {k: est[f"cps_hat:{k}"] for k in enough}
    if tau > 0.0:
        reliable = [k for k in enough if lower[k] >= tau]
        if d not in reliable:
            if not reliable:
                why = f"no action demonstrably reaches success {tau}"
                return d, f"[{MODE_DEFERRED}] {why}; rule baseline {d}.", est, MODE_DEFERRED
            best = min(reliable, key=lambda k: (cost[k], k))
            return (
                best,
                f"[{MODE_GREEDY}] default {d} is not demonstrably >= {tau} success; {best} is "
                f"(lower bound {lower[best]:.2f}) and the cheapest such action.",
                est,
                MODE_GREEDY,
            )
        pool = reliable
    else:
        pool = enough
    admissible = [d]
    for k in pool:
        if k == d:
            continue
        lo, _ = newcombe_difference_bounds(ev[k].successes, ev[k].n, ev[d].successes, ev[d].n, conf)
        if lo >= -delta:
            admissible.append(k)
    best = min(admissible, key=lambda k: (cost[k], k))
    if best != d and cost[best] <= (1.0 - params.min_cost_saving) * cost[d]:
        return (
            best,
            f"[{MODE_GREEDY}] {best} is non-inferior to {d} (margin {delta}) and cheaper "
            f"({cost[best]:.0f} vs {cost[d]:.0f} tokens per verified success).",
            est,
            MODE_GREEDY,
        )
    return (
        d,
        f"[{MODE_GREEDY}] keep {d}: no admissible action saves >= "
        f"{params.min_cost_saving:.0%} per verified success.",
        est,
        MODE_GREEDY,
    )


def _explorable(f: DecisionFeatures, params: ControllerParams, greedy: str) -> list[str]:
    """Actions exploration may draw from; empty when exploration is not allowed here."""
    if (
        f.controller_kind != ControllerKind.BANDIT
        or params.epsilon <= 0.0
        or f.u is None
        or f.risk_level not in params.exploration_risk_levels
        or f.task_family not in params.exploration_families
        or not f.calibration_ok
        or f.n_verified_total < params.min_total_verified
    ):
        return []
    ev: dict[str, EvidenceView] = {e.key: e for e in f.evidence}
    d = f.default_key
    if d not in ev or ev[d].n < params.n_min:
        return []
    p_d = p_hat(ev[d].successes, ev[d].n)
    delta = params.delta[f.risk_level]
    out = {greedy, d}
    for a in f.actions:
        e = ev.get(a.key)
        confidently_worse = (
            e is not None
            and e.n >= params.n_min
            and wilson_one_sided(e.successes, e.n, params.confidence)[1] < p_d - delta
        )
        if not confidently_worse:
            out.add(a.key)
    return sorted(out)


def choose(f: DecisionFeatures, params: ControllerParams) -> Choice:
    """The whole policy. Pure: depends only on `f` and `params`."""
    keys = [a.key for a in f.actions]
    if f.protected:
        return _deferred(f, f"protected risk class {f.risk_level!r} (learned policy not allowed)")
    if f.tenant_mismatch:
        return _deferred(f, "task tenant differs from the policy's tenant")
    if len(keys) == 1:
        return _deferred(f, "single feasible action")
    greedy, reason, est, greedy_mode = _greedy(f, params)
    pool = _explorable(f, params, greedy)
    if len(pool) < 2 or f.u is None:
        return Choice(greedy, 1.0, {greedy: 1.0}, est, reason, greedy_mode)
    eps = params.epsilon
    m = len(pool)
    props = {k: eps / m for k in pool}
    props[greedy] += 1.0 - eps
    if f.u < eps:
        selected = pool[min(int(f.u / eps * m), m - 1)]
        mode = MODE_EXPLORE
        why = (
            f"[{MODE_EXPLORE}] epsilon={eps} draw u={f.u:.4f} picked {selected} among {m} "
            f"explorable actions (greedy would be {greedy})."
        )
    else:
        selected, mode, why = greedy, greedy_mode, reason
    est = dict(est)
    for k, v in props.items():
        est[f"pi:{k}"] = round(v, 6)
    return Choice(selected, props[selected], props, est, why, mode)


class AdaptiveController:
    """`Controller` backed by one frozen policy definition (cost-aware or bandit)."""

    def __init__(self, definition: ControllerPolicyDefinition):
        self.definition = definition
        self.policy_version = definition.policy_version
        self._snapshot_hash = definition.snapshot.snapshot_hash()

    def decide(self, ctx: ControllerInput) -> ControllerDecision:
        f = build_features(ctx, self.definition, self._snapshot_hash)
        choice = choose(f, self.definition.params)
        return decision_from(ctx, f, choice, self.policy_version)


def decision_from(
    ctx: ControllerInput,
    f: DecisionFeatures,
    choice: Choice,
    policy_version: str,
    acted: bool = True,
) -> ControllerDecision:
    strategy, ref = parse_action_key(choice.selected_key)
    match = ctx.match
    snapshot: dict[str, object] = {
        "task_family": ctx.task.task_family,
        "risk_level": ctx.task.risk_level,
        "n_allowed_tools": len(ctx.task.allowed_tools),
        "n_catalog": match.n_catalog if match else 0,
        "n_eligible_capabilities": len(match.eligible) if match else 0,
        "candidates": match.snapshot() if match else [],
        CONTROLLER_KEY: f.model_dump(mode="json"),
    }
    if ref is not None:
        snapshot[SELECTED_CAPABILITY_KEY] = ref
    strategies: list[Strategy] = []
    for a in f.actions:
        if a.strategy not in strategies:
            strategies.append(a.strategy)
    return ControllerDecision(
        decision_id=ctx.decision_id,
        run_id=ctx.run_id,
        tenant_id=ctx.task.tenant_id,
        policy_version=policy_version,
        feature_snapshot=snapshot,
        eligible_actions=strategies,
        estimates=choice.estimates,
        selected_action=strategy,
        selection_probability=choice.probability,
        reason=choice.reason,
        acted=acted,
    )
