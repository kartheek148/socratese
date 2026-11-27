"""Decision replay (reproducibility) and off-policy value estimation on logged decisions."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from experience_engine.controller.estimator import cost_hat, p_hat
from experience_engine.controller.features import explore_draw, features_for
from experience_engine.controller.policy import Choice, choose
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    ControllerPolicyDefinition,
    DecisionFeatures,
    DecisionOutcomeRecord,
    EstimatorSnapshot,
    PairedCell,
    action_key,
    cell_key,
    paired_key,
)
from experience_engine.domain.decision import ControllerDecision
from experience_engine.execution.controller import SELECTED_CAPABILITY_KEY


@dataclass(frozen=True)
class ReplayCheck:
    reproducible: bool
    differences: list[str] = field(default_factory=list)


def selected_key_of(decision: ControllerDecision) -> str:
    ref = decision.feature_snapshot.get(SELECTED_CAPABILITY_KEY)
    return action_key(decision.selected_action, ref if isinstance(ref, str) else None)


def replay_decision(
    decision: ControllerDecision, definition: ControllerPolicyDefinition
) -> ReplayCheck:
    """Recompute a stored decision from its stored features and the policy version alone."""
    diffs: list[str] = []
    if decision.policy_version != definition.policy_version:
        msg = f"policy version {decision.policy_version!r} != {definition.policy_version!r}"
        return ReplayCheck(False, [msg])
    raw = decision.feature_snapshot.get(CONTROLLER_KEY)
    if not isinstance(raw, dict):
        return ReplayCheck(
            False, ["decision has no controller features (not an adaptive decision)"]
        )
    f = DecisionFeatures.model_validate(raw)
    if f.snapshot_hash != definition.snapshot.snapshot_hash():
        diffs.append("estimator snapshot hash differs from the registered definition")
    if f.u is not None and f.u != explore_draw(decision.decision_id, decision.policy_version):
        diffs.append("exploration draw is not the deterministic function of the decision id")
    for e in f.evidence:
        cell = definition.snapshot.cells.get(cell_key(f.task_family, f.risk_level, e.key))
        if cell is None or (cell.n, cell.successes, cell.token_sum) != (
            e.n,
            e.successes,
            e.token_sum,
        ):
            diffs.append(f"evidence for {e.key} does not match the registered snapshot")
    for pv in f.paired:
        pcell = definition.snapshot.paired.get(
            paired_key(f.task_family, f.risk_level, f.default_key, pv.key)
        )
        if pcell is None or PairedCell(**pv.model_dump(exclude={"key"})) != pcell:
            diffs.append(f"paired prior for {pv.key} does not match the registered snapshot")
    if f.evidence_module != (definition.params.evidence is not None):
        diffs.append("evidence-module flag differs from the registered definition")
    c = choose(f, definition.params)
    if c.selected_key != selected_key_of(decision):
        diffs.append(f"selected {selected_key_of(decision)!r}, recomputed {c.selected_key!r}")
    if c.probability != decision.selection_probability:
        diffs.append(f"probability {decision.selection_probability} != {c.probability}")
    if c.estimates != decision.estimates:
        diffs.append("estimates differ from the recomputation")
    if c.reason != decision.reason:
        diffs.append("reason differs from the recomputation")
    return ReplayCheck(not diffs, diffs)


# ---- off-policy estimation ---------------------------------------------------------------------
@dataclass(frozen=True)
class OffPolicyEstimate:
    n: int
    coverage: float  # mean candidate probability mass on actions with logged support
    agreement: float  # share of decisions where the candidate's most likely action was acted
    logged_success: float
    logged_tokens: float
    ips_success: float | None
    snips_success: float | None
    dm_success: float | None
    dr_success: float | None
    ips_tokens: float | None
    dm_tokens: float | None
    dr_tokens: float | None
    protected_deviations: int  # protected-risk records where the candidate differs from the rule
    infeasible_choices: int  # choices outside the logged feasible set


def candidate_choice(
    definition: ControllerPolicyDefinition, snapshot_hash: str, r: DecisionOutcomeRecord
) -> Choice:
    f = features_for(
        definition,
        snapshot_hash,
        decision_id=r.decision_id,
        tenant_id=r.tenant_id,
        family=r.family,
        risk=r.risk_level,
        n_allowed_tools=r.n_allowed_tools,
        actions=r.actions,
        default_key=r.default_key,
    )
    return choose(f, definition.params)


def off_policy_estimate(
    records: Sequence[DecisionOutcomeRecord],
    definition: ControllerPolicyDefinition,
    reward_model: EstimatorSnapshot,
) -> OffPolicyEstimate:
    """IPS / SNIPS / direct / doubly-robust value of `definition` on logged acted decisions.

    Importance weights use the logged selection probability of the acted action; support is the
    set of actions with a logged positive probability. `reward_model` supplies per-cell success
    and cost estimates for the direct and doubly-robust terms. Unsupported candidate mass is
    *not* imputed by IPS (it contributes 0), which is why `coverage` must be read with it.
    """
    h = definition.snapshot.snapshot_hash()
    use = [r for r in records if r.acted and r.known and r.actions]
    n = len(use)
    prior_by_ctx: dict[str, float] = {}

    def prior(fam: str | None, risk: str) -> float:
        key = f"{fam or '-'}|{risk}"
        if key not in prior_by_ctx:
            cs = [c for k, c in reward_model.cells.items() if k.startswith(key + "|")]
            tot = sum(c.n for c in cs)
            prior_by_ctx[key] = sum(c.token_sum for c in cs) / tot if tot else 0.0
        return prior_by_ctx[key]

    w = cov = agree = 0.0
    ips_s = ips_t = sn_num = sn_den = dm_s = dm_t = dr_s = dr_t = 0.0
    prot = infeasible = 0
    for r in use:
        ch = candidate_choice(definition, h, r)
        keys = {a.key for a in r.actions}
        if any(k not in keys for k in ch.propensities):
            infeasible += 1
        if (
            r.risk_level not in definition.params.learned_risk_levels
            and ch.selected_key != r.default_key
        ):
            prot += 1
        mu = r.propensities or {r.action: r.selection_probability or 1.0}
        support = sum(p for k, p in ch.propensities.items() if mu.get(k, 0.0) > 0.0)
        cov += support
        top = max(ch.propensities.items(), key=lambda kv: (kv[1], kv[0]))[0]
        agree += float(top == r.action)
        y_s, y_t = float(r.success), float(r.tokens)

        def model(k: str, r: DecisionOutcomeRecord = r) -> tuple[float, float]:
            cell = reward_model.cells.get(cell_key(r.family, r.risk_level, k))
            pr = prior(r.family, r.risk_level)
            if cell is None:
                return 0.5, pr
            return p_hat(cell.successes, cell.n), cost_hat(
                cell, pr, definition.params.cost_prior_weight
            )

        dm_s_i = dm_t_i = 0.0
        for k, pi in ch.propensities.items():
            ps, ct = model(k)
            dm_s_i += pi * ps
            dm_t_i += pi * ct
        pi_a = ch.propensities.get(r.action, 0.0)
        mu_a = mu.get(r.action, r.selection_probability or 1.0)
        wt = pi_a / mu_a if mu_a > 0 else 0.0
        ps_a, ct_a = model(r.action)
        w += wt
        ips_s += wt * y_s
        ips_t += wt * y_t
        sn_num += wt * y_s
        sn_den += wt
        dm_s += dm_s_i
        dm_t += dm_t_i
        dr_s += dm_s_i + wt * (y_s - ps_a)
        dr_t += dm_t_i + wt * (y_t - ct_a)

    def avg(x: float) -> float | None:
        return x / n if n else None

    return OffPolicyEstimate(
        n=n,
        coverage=cov / n if n else 0.0,
        agreement=agree / n if n else 0.0,
        logged_success=sum(r.success for r in use) / n if n else 0.0,
        logged_tokens=sum(r.tokens for r in use) / n if n else 0.0,
        ips_success=avg(ips_s),
        snips_success=(sn_num / sn_den) if sn_den > 0 else None,
        dm_success=avg(dm_s),
        dr_success=avg(dr_s),
        ips_tokens=avg(ips_t),
        dm_tokens=avg(dm_t),
        dr_tokens=avg(dr_t),
        protected_deviations=prot,
        infeasible_choices=infeasible,
    )
