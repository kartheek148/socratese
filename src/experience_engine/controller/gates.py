"""Stage evidence for the controller rollout: offline replay, shadow, canary.

Each evaluator returns a `StageReport` with a three-valued verdict (PASS / FAIL / INCONCLUSIVE),
the metrics behind it and every unmet reason. None of them changes any state; only the registry
applies a verdict. Cost can raise "improves" in a report but never compensates a failed gate.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.controller.calibration import brier, expected_calibration_error
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.replay import off_policy_estimate, selected_key_of
from experience_engine.domain.controller import (
    ControllerPolicyDefinition,
    DecisionOutcomeRecord,
)
from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.rollout import (
    CanaryGates,
    Metric,
    OfflineGates,
    RolloutGates,
    ShadowGates,
    Stage,
    StageReport,
)
from experience_engine.domain.stats import newcombe_difference_bounds


def _report(
    stage: Stage,
    definition: ControllerPolicyDefinition,
    gates: RolloutGates,
    verdict: Verdict,
    metrics: dict[str, Metric],
    reasons: list[str],
) -> StageReport:
    return StageReport(
        stage=stage,
        verdict=verdict,
        policy_version=definition.policy_version,
        definition_hash=definition.definition_hash(),
        gate_hash=gates.gate_hash(stage),
        metrics=metrics,
        reasons=reasons,
    )


# ---- offline ------------------------------------------------------------------------------------
def evaluate_offline(
    definition: ControllerPolicyDefinition,
    logs: Sequence[DecisionOutcomeRecord],
    gates: RolloutGates | None = None,
) -> StageReport:
    """Replay the candidate over logged decisions (what it would have chosen) with the gates."""
    gates = gates or RolloutGates()
    g: OfflineGates = gates.offline
    est = off_policy_estimate(logs, definition, build_snapshot(logs, definition.tenant_id))
    cal = definition.snapshot.calibration
    metrics: dict[str, Metric] = {
        "n_decisions": est.n,
        "coverage": round(est.coverage, 4),
        "agreement": round(est.agreement, 4),
        "logged_success": round(est.logged_success, 4),
        "dr_success": None if est.dr_success is None else round(est.dr_success, 4),
        "logged_tokens": round(est.logged_tokens, 1),
        "dr_tokens": None if est.dr_tokens is None else round(est.dr_tokens, 1),
        "protected_deviations": est.protected_deviations,
        "infeasible_choices": est.infeasible_choices,
        "calibration_n": cal.n if cal else 0,
        "calibration_ece": cal.ece if cal else None,
        "calibration_skill": cal.brier_skill if cal else None,
    }
    fail: list[str] = []
    pending: list[str] = []
    if est.protected_deviations > g.max_protected_deviations:
        fail.append(
            f"{est.protected_deviations} protected-class deviation(s) from the rule baseline"
        )
    if est.infeasible_choices > g.max_infeasible_choices:
        fail.append(f"{est.infeasible_choices} choice(s) outside the feasible set")
    if cal is None or cal.n < g.min_decisions:
        scored = 0 if cal is None else cal.n
        pending.append(f"only {scored} scored calibration predictions (< {g.min_decisions})")
    elif not cal.ok:
        fail.append(f"estimator calibration gate not met: {'; '.join(cal.reasons)}")
    if (
        est.coverage >= g.min_coverage_for_harm_check
        and est.dr_success is not None
        and est.dr_success < est.logged_success - g.harm_margin
    ):
        fail.append(
            f"predicted success {est.dr_success:.3f} is more than {g.harm_margin} below the "
            f"logged {est.logged_success:.3f}"
        )
    if est.n < g.min_decisions:
        pending.append(f"only {est.n} logged decisions (< {g.min_decisions})")
    if fail:
        return _report(Stage.OFFLINE, definition, gates, Verdict.FAIL, metrics, fail + pending)
    if pending:
        return _report(Stage.OFFLINE, definition, gates, Verdict.INCONCLUSIVE, metrics, pending)
    if est.coverage < g.min_coverage_for_harm_check:
        metrics["counterfactual_evidence"] = (
            "none: logged propensities do not support the candidate"
        )
    return _report(Stage.OFFLINE, definition, gates, Verdict.PASS, metrics, [])


# ---- shadow -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ShadowPair:
    """The acted run (with its verified outcome) and the candidate's non-acted decision."""

    acted: DecisionOutcomeRecord
    shadow: ControllerDecision


def evaluate_shadow(
    definition: ControllerPolicyDefinition,
    pairs: Sequence[ShadowPair],
    decision_errors: int = 0,
    gates: RolloutGates | None = None,
) -> StageReport:
    gates = gates or RolloutGates()
    g: ShadowGates = gates.shadow
    known = [p for p in pairs if p.acted.known]
    agree = sum(selected_key_of(p.shadow) == p.acted.action for p in known)
    protected = sum(
        1
        for p in known
        if p.acted.risk_level not in definition.params.learned_risk_levels
        and selected_key_of(p.shadow) != p.acted.default_key
    )
    preds: list[float] = []
    obs: list[bool] = []
    for p in known:
        v = p.shadow.estimates.get(f"p_hat:{p.acted.action}")
        if v is not None:
            preds.append(v)
            obs.append(p.acted.success)
    ece = expected_calibration_error(preds, obs) if preds else None
    saving = []
    for p in known:
        a = p.shadow.estimates.get(f"cps_hat:{selected_key_of(p.shadow)}")
        b = p.shadow.estimates.get(f"cps_hat:{p.acted.action}")
        if a is not None and b is not None and selected_key_of(p.shadow) != p.acted.action:
            saving.append(b - a)
    metrics: dict[str, Metric] = {
        "n_decisions": len(known),
        "agreement": round(agree / len(known), 4) if known else None,
        "n_scored_predictions": len(preds),
        "live_ece": None if ece is None else round(ece, 4),
        "live_brier": round(brier(preds, obs), 4) if preds else None,
        "protected_deviations": protected,
        "decision_errors": decision_errors,
        "disagreements_with_model_estimate": len(saving),
        "model_est_tokens_per_success_saved_per_disagreement": round(sum(saving) / len(saving), 1)
        if saving
        else None,
    }
    fail: list[str] = []
    pending: list[str] = []
    if protected > g.max_protected_deviations:
        fail.append(f"{protected} protected-class deviation(s) from the acted rule decision")
    if decision_errors > g.max_decision_errors:
        fail.append(f"{decision_errors} shadow decision error(s)")
    if ece is not None and len(preds) >= g.min_decisions and ece > g.max_ece:
        fail.append(f"live ECE {ece:.3f} > {g.max_ece}")
    if len(known) < g.min_decisions:
        pending.append(
            f"only {len(known)} shadow decisions with verified outcomes (< {g.min_decisions})"
        )
    if len(preds) < g.min_decisions:
        pending.append(f"only {len(preds)} scored live predictions (< {g.min_decisions})")
    if fail:
        return _report(Stage.SHADOW, definition, gates, Verdict.FAIL, metrics, fail + pending)
    if pending:
        return _report(Stage.SHADOW, definition, gates, Verdict.INCONCLUSIVE, metrics, pending)
    return _report(Stage.SHADOW, definition, gates, Verdict.PASS, metrics, [])


# ---- canary -------------------------------------------------------------------------------------
def canary_assignment(task_id: str, fraction: float, salt: str = "canary") -> bool:
    """Stable per-task split: the same task id always lands in the same arm."""
    h = int(hashlib.sha256(f"{salt}|{task_id}".encode()).hexdigest()[:12], 16) / 16**12
    return h < fraction


def evaluate_canary(
    candidate: ControllerPolicyDefinition,
    control_policy_version: str,
    rows: Sequence[DecisionOutcomeRecord],
    gates: RolloutGates | None = None,
    *,
    control_adaptive_version: str | None = None,
) -> StageReport:
    """Concurrent comparison of acted runs by the candidate vs the control controller.

    `rows` must come from the canary window only (decisions made while the candidate was in
    CANARY): the caller filters by time. The control arm is further restricted to the risk classes
    the candidate may serve, so both arms are drawn from the same population (protected tasks are
    only ever served by the control and must not dilute the comparison).

    `control_policy_version` selects the control's rows (e.g. `rule-capability/1`).
    `control_adaptive_version` is the registered ACTIVE adaptive version the control was, or None
    when the control was the rule baseline; the registry requires it to equal the version that is
    ACTIVE at promotion time, so evidence gathered against a stale control cannot be reused."""
    gates = gates or RolloutGates()
    g: CanaryGates = gates.canary
    cand = [r for r in rows if r.acted and r.known and r.policy_version == candidate.policy_version]
    learned = set(candidate.params.learned_risk_levels)
    ctrl = [
        r
        for r in rows
        if r.acted
        and r.known
        and r.policy_version == control_policy_version
        and r.risk_level in learned
    ]
    exposure = sum(1 for r in cand if r.risk_level not in candidate.params.learned_risk_levels)
    cs, cn = sum(r.success for r in cand), len(cand)
    ks, kn = sum(r.success for r in ctrl), len(ctrl)
    lo, hi = newcombe_difference_bounds(cs, cn, ks, kn, g.confidence)
    tps_c = sum(r.tokens for r in cand) / cs if cs else None
    tps_k = sum(r.tokens for r in ctrl) / ks if ks else None
    ratio = tps_c / tps_k if tps_c is not None and tps_k else None
    deviations = sum(1 for r in cand if r.action != r.default_key)
    metrics: dict[str, Metric] = {
        "n_candidate": cn,
        "n_control": kn,
        "success_candidate": round(cs / cn, 4) if cn else None,
        "success_control": round(ks / kn, 4) if kn else None,
        "diff_lower": round(lo, 4),
        "diff_upper": round(hi, 4),
        "tokens_per_success_ratio": None if ratio is None else round(ratio, 4),
        "candidate_deviation_rate": round(deviations / cn, 4) if cn else None,
        "protected_exposures": exposure,
        "control_version": control_adaptive_version,
    }
    fail: list[str] = []
    pending: list[str] = []
    if exposure > g.max_protected_exposures:
        fail.append(f"{exposure} protected-class task(s) reached the candidate")
    if cn >= g.min_per_arm and kn >= g.min_per_arm and hi < 0.0:
        fail.append(
            f"candidate success is significantly lower (difference upper bound {hi:.3f} < 0)"
        )
    if cn < g.min_per_arm or kn < g.min_per_arm:
        pending.append(f"arms {cn}/{kn} below the minimum of {g.min_per_arm}")
    elif not fail and lo < -g.noninferiority_margin:
        margin = g.noninferiority_margin
        pending.append(f"non-inferiority not shown (difference lower bound {lo:.3f} < -{margin})")
    if fail:
        return _report(Stage.CANARY, candidate, gates, Verdict.FAIL, metrics, fail + pending)
    if pending:
        return _report(Stage.CANARY, candidate, gates, Verdict.INCONCLUSIVE, metrics, pending)
    return _report(Stage.CANARY, candidate, gates, Verdict.PASS, metrics, [])


__all__ = [
    "ShadowPair",
    "canary_assignment",
    "evaluate_canary",
    "evaluate_offline",
    "evaluate_shadow",
]
