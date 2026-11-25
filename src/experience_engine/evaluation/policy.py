"""Promotion policy loading and the verdict function (pure).

The verdict is a conjunction of hard gates - correctness, applicability, regression and (when
configured) cost. Cost can never buy back a correctness failure. Sample-size shortfalls produce
INCONCLUSIVE, never PASS.
"""

import json
from collections.abc import Mapping
from pathlib import Path

from experience_engine.domain.evaluation import (
    Arm,
    ArmMetrics,
    PromotionPolicy,
    Suite,
    Verdict,
    default_policies,
)
from experience_engine.domain.stats import wilson_interval

Suites = Mapping[str, Mapping[str, ArmMetrics]]


def load_policies(path: Path | None) -> dict[str, PromotionPolicy]:
    """Policies from a reviewed JSON file ({"low": {...}, "medium": {...}, "high": {...}}), or the
    built-in defaults. All three risk classes must be defined so none silently falls back."""
    if path is None:
        return default_policies()
    raw = json.loads(path.read_text())
    policies = {k: PromotionPolicy.model_validate(v) for k, v in raw.items()}
    missing = {"low", "medium", "high"} - set(policies)
    if missing:
        raise ValueError(f"policy file must define all risk classes; missing {sorted(missing)}")
    for risk, p in policies.items():
        if p.risk_class != risk:
            raise ValueError(
                f"policy {p.name!r} is filed under {risk!r} but declares {p.risk_class!r}"
            )
    return policies


def pooled(suites: Suites, arm: Arm, names: tuple[Suite, ...]) -> tuple[int, int, int, int]:
    """(n, verified, tokens_total, tool_calls_total) pooled over the given suites for one arm."""
    n = verified = tokens = calls = 0
    for s in names:
        m = suites.get(s.value, {}).get(arm.value)
        if m:
            n += m.n
            verified += m.verified
            tokens += m.tokens_total
            calls += m.tool_calls_total
    return n, verified, tokens, calls


def success_lower_bound(suites: Suites, treatment: Arm = Arm.CAPABILITY) -> float | None:
    n, ok, _, _ = pooled(suites, treatment, (Suite.HELD_OUT, Suite.TRANSFER))
    return wilson_interval(ok, n)[0] if n else None


def decide_verdict(
    policy: PromotionPolicy,
    suites: Suites,
    *,
    false_applicability_rate: float | None,
    regressions: int,
    treatment: Arm = Arm.CAPABILITY,
) -> tuple[Verdict, list[str]]:
    def cap(suite: Suite) -> ArmMetrics | None:
        return suites.get(suite.value, {}).get(treatment.value)

    held, transfer = cap(Suite.HELD_OUT), cap(Suite.TRANSFER)
    neg, source = cap(Suite.NEGATIVE), cap(Suite.SOURCE_REPLAY)
    shortfalls: list[str] = []
    for label, m, need in (
        ("held-out", held, policy.min_heldout_n),
        ("transfer", transfer, policy.min_transfer_n),
        ("negative-applicability", neg, policy.min_negative_n),
    ):
        # sample-size gates count distinct tasks: repeating a task over seeds adds precision
        # about the model's variance, not new evidence about the capability
        have = (m.n_tasks if m.n_tasks is not None else m.n) if m else 0
        if have < need:
            shortfalls.append(f"{label} suite has {have} tasks; policy requires at least {need}")
    if shortfalls or held is None or transfer is None or neg is None:
        return Verdict.INCONCLUSIVE, shortfalls

    failures: list[str] = []
    if source is None or source.rate < policy.min_source_replay_success_rate:
        got = 0.0 if source is None else source.rate
        failures.append(
            f"source replay success {got:.0%} < {policy.min_source_replay_success_rate:.0%} "
            "(necessary, not sufficient)"
        )
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
    if (
        false_applicability_rate is not None
        and false_applicability_rate > policy.max_false_applicability_rate
    ):
        failures.append(
            f"false applicability {false_applicability_rate:.0%} "
            f"> {policy.max_false_applicability_rate:.0%}"
        )
    if regressions > policy.max_regressions:
        failures.append(f"{regressions} regressions vs baseline > allowed {policy.max_regressions}")
    if policy.max_exec_token_ratio is not None:
        _, ok_c, tok_c, _ = pooled(suites, treatment, (Suite.HELD_OUT, Suite.TRANSFER))
        _, ok_a, tok_a, _ = pooled(suites, Arm.BASELINE, (Suite.HELD_OUT, Suite.TRANSFER))
        if ok_c and ok_a and tok_a:
            ratio = (tok_c / ok_c) / (tok_a / ok_a)
            if ratio > policy.max_exec_token_ratio:
                failures.append(
                    f"execution tokens per success are {ratio:.2f}x baseline "
                    f"> {policy.max_exec_token_ratio}x"
                )
    return (
        (Verdict.FAIL, failures) if failures else (Verdict.PASS, ["all promotion gates satisfied"])
    )
