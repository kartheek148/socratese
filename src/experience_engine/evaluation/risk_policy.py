"""Risk-based verdict (`*-v2`, M5.9) and the policy dispatcher (pure).

Gates are a conjunction (intersection-union test): every gate must hold at its own level, so
promotion needs no alpha splitting. Statistical gates are three-valued: PASS when the evidence
shows the requirement holds, FAIL when it shows it does not, INCONCLUSIVE otherwise. Token savings
never enter any gate except the optional token-ratio gate, which can only fail an asset.
"""

from experience_engine.domain.evaluation import (
    AnyPolicy,
    Arm,
    RiskBasedPolicy,
    RiskEvidence,
    Suite,
    Verdict,
)
from experience_engine.evaluation.policy import (
    Suites,
    decide_verdict,
    success_lower_bound,
    token_ratio_failure,
)
from experience_engine.evaluation.risk import (
    cp_lower,
    cp_upper,
    paired_bootstrap_bounds,
)


def critical_violations(policy: RiskBasedPolicy, evidence: RiskEvidence) -> int:
    return sum(
        n
        for term, n in evidence.termination_counts.items()
        if any(term == c or term.startswith(f"{c}:") for c in policy.critical_terminations)
    )


Gate = tuple[str, str]  # ("pass" | "fail" | "open", description)


def regression_gate(policy: RiskBasedPolicy, evidence: RiskEvidence) -> Gate:
    """G1: P(candidate fails | baseline succeeds) bounded at the task level."""
    delta, n_t, x = (
        policy.max_regression_rate_ucb,
        evidence.regression_tasks,
        evidence.regression_mass,
    )
    conf = policy.confidence
    if n_t == 0:
        return "open", "no baseline-solved tasks: regression risk cannot be bounded"
    ucb, lcb = cp_upper(x, n_t, conf), cp_lower(x, n_t, conf)
    desc = (
        f"regression rate {x / n_t:.0%} over {n_t} baseline-solved tasks "
        f"({evidence.n_regressions}/{evidence.n_pairs} runs), "
        f"{conf:.1%} bounds [{lcb:.0%}, {ucb:.0%}]"
    )
    if lcb > delta:
        return "fail", f"{desc}: above tolerance {delta:.0%}"
    if ucb > delta:
        return "open", f"{desc}: cannot show <= {delta:.0%}; more evidence needed"
    if n_t < policy.min_regression_tasks:
        return (
            "open",
            f"{desc}: only {n_t} baseline-solved tasks; policy requires "
            f"{policy.min_regression_tasks}",
        )
    return "pass", desc


def noninferiority_gate(policy: RiskBasedPolicy, evidence: RiskEvidence) -> Gate:
    """G2: paired task-level success difference against the baseline."""
    if not evidence.paired_diffs:
        return "open", "no paired task evidence for non-inferiority"
    conf, margin = policy.confidence, policy.noninferiority_margin
    mean, lo, hi = paired_bootstrap_bounds(
        evidence.paired_diffs,
        conf,
        resamples=policy.bootstrap_resamples,
        seed=policy.bootstrap_seed,
    )
    desc = (
        f"paired success difference {mean:+.0%} over {len(evidence.paired_diffs)} tasks, "
        f"{conf:.1%} bounds [{lo:+.0%}, {hi:+.0%}]"
    )
    if hi < -margin:
        return "fail", f"{desc}: worse than baseline by more than {margin:.0%}"
    if lo < -margin:
        return "open", f"{desc}: cannot show non-inferiority margin {margin:.0%}"
    return "pass", desc


def variant_gates(policy: RiskBasedPolicy, evidence: RiskEvidence) -> list[Gate]:
    """G7: a variant with a high regression rate fails even if the aggregate is fine."""
    out: list[Gate] = []
    for variant, (pairs, regs) in evidence.variant_pairs.items():
        if pairs >= policy.min_variant_pairs and regs / pairs > policy.max_variant_regression_rate:
            out.append(
                (
                    "fail",
                    f"variant {variant}: {regs}/{pairs} baseline-solved runs regressed "
                    f"(> {policy.max_variant_regression_rate:.0%})",
                )
            )
    return out


def decide_verdict_v2(
    policy: RiskBasedPolicy,
    suites: Suites,
    *,
    false_applicability_rate: float | None,
    evidence: RiskEvidence,
    treatment: Arm = Arm.CAPABILITY,
    require_paired: bool = True,  # False only for retrospective simulation without task-level rows
) -> tuple[Verdict, list[str]]:
    def cap(suite: Suite):  # type: ignore[no-untyped-def]
        return suites.get(suite.value, {}).get(treatment.value)

    held, transfer, neg = cap(Suite.HELD_OUT), cap(Suite.TRANSFER), cap(Suite.NEGATIVE)
    shortfalls: list[str] = []
    for label, m, need in (
        ("held-out", held, policy.min_heldout_n),
        ("transfer", transfer, policy.min_transfer_n),
        ("negative-applicability", neg, policy.min_negative_n),
    ):
        have = (m.n_tasks if m.n_tasks is not None else m.n) if m else 0
        if have < need:
            shortfalls.append(f"{label} suite has {have} tasks; policy requires at least {need}")
    if shortfalls or held is None or transfer is None or neg is None:
        return Verdict.INCONCLUSIVE, shortfalls

    failures: list[str] = []
    open_: list[str] = []
    conf = policy.confidence

    critical = critical_violations(policy, evidence)
    if critical > policy.max_critical_violations:
        failures.append(
            f"{critical} critical violation(s) ({', '.join(policy.critical_terminations)}) "
            f"> allowed {policy.max_critical_violations}"
        )
    if (
        false_applicability_rate is not None
        and false_applicability_rate > policy.max_false_applicability_rate
    ):
        failures.append(
            f"false applicability {false_applicability_rate:.0%} "
            f"> {policy.max_false_applicability_rate:.0%}"
        )

    # G3 source replay: fail only when the evidence excludes meeting the requirement
    need_src = policy.min_source_replay_success_rate
    if evidence.source_tasks == 0:
        open_.append("no source-replay evidence")
    else:
        rate = evidence.source_mass / evidence.source_tasks
        ucb = cp_upper(evidence.source_mass, evidence.source_tasks, conf)
        if ucb < need_src:
            failures.append(
                f"source replay success {rate:.0%} (upper bound {ucb:.0%}) excludes the "
                f"required {need_src:.0%} (necessary, not sufficient)"
            )

    # G4 held-out / transfer success and the Wilson lower bound (as v1)
    if held.rate < policy.min_heldout_success_rate:
        failures.append(f"held-out success {held.rate:.0%} < {policy.min_heldout_success_rate:.0%}")
    if transfer.rate < policy.min_transfer_success_rate:
        failures.append(
            f"transfer success {transfer.rate:.0%} < {policy.min_transfer_success_rate:.0%}"
        )
    lb = success_lower_bound(suites, treatment)
    if lb is not None and lb < policy.min_success_lower_bound:
        failures.append(
            f"95% lower bound on held-out+transfer success {lb:.0%} "
            f"< {policy.min_success_lower_bound:.0%}"
        )

    for gate in (
        regression_gate(policy, evidence),
        noninferiority_gate(policy, evidence) if require_paired else None,
        *variant_gates(policy, evidence),
    ):
        if gate is None:
            continue
        status, text = gate
        if status == "fail":
            failures.append(text)
        elif status == "open":
            open_.append(text)

    token_failure = token_ratio_failure(policy, suites, treatment)
    if token_failure:
        failures.append(token_failure)

    if failures:
        return Verdict.FAIL, failures
    if open_:
        return Verdict.INCONCLUSIVE, open_
    return Verdict.PASS, ["all promotion gates satisfied"]


def decide(
    policy: AnyPolicy,
    suites: Suites,
    *,
    false_applicability_rate: float | None,
    regressions: int,
    evidence: RiskEvidence,
    treatment: Arm = Arm.CAPABILITY,
) -> tuple[Verdict, list[str]]:
    """Dispatch on the policy kind. v1 policies ignore the risk evidence entirely."""
    if isinstance(policy, RiskBasedPolicy):
        return decide_verdict_v2(
            policy,
            suites,
            false_applicability_rate=false_applicability_rate,
            evidence=evidence,
            treatment=treatment,
        )
    return decide_verdict(
        policy,
        suites,
        false_applicability_rate=false_applicability_rate,
        regressions=regressions,
        treatment=treatment,
    )
