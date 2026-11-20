from experience_engine.bench.report import assert_no_secrets
from experience_engine.domain.evaluation import Arm, ArmMetrics, EvaluationReport, Suite


def _fmt(v: float | None, digits: int = 0) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _row(label: str, arm: str, m: ArmMetrics) -> str:
    extra = ""
    if m.applicable is not None:
        extra = f"applicable {m.applicable}/{m.n}"
    return (
        f"| {label} | {arm} | {m.n} | {m.verified} | {m.rate:.0%} ({m.ci_low:.0%}-{m.ci_high:.0%}) "
        f"| {_fmt(m.tokens_per_success)} | {_fmt(m.tool_calls_per_success, 1)} "
        f"| {m.latency_mean_ms:.0f} | {extra} |"
    )


def render_evaluation(report: EvaluationReport) -> str:
    text = _render(report)
    assert_no_secrets(text)
    return text


def _render(report: EvaluationReport) -> str:
    """Markdown report. Contains aggregates and task ids only, no task contents or secrets."""
    lc = report.lifecycle
    lines = [
        f"# Evaluation: {report.capability_id} v{report.version}",
        "",
        f"**Verdict: {report.verdict.value}** (risk class `{report.risk_class}`, "
        f"policy `{report.policy_name}` hash `{report.policy_hash[:12]}…`)",
        "",
        "Reasons:",
        *[f"- {r}" for r in report.reasons],
        "",
        "## Reproducibility metadata",
        "",
        *[
            f"- {split}: `{v['name']}` v{v['version']} hash `{v['content_hash'][:12]}…`"
            for split, v in sorted(report.dataset_versions.items())
        ],
        f"- Model: `{report.model_settings.get('provider')}` / "
        f"`{report.model_settings.get('model')}`; "
        f"seeds {report.seeds}",
        f"- Runtime `{report.runtime_version}`, verifier `{report.verifier_version}`, "
        f"environment `{report.environment_version}`, evaluator `{report.evaluator_version}`",
        "",
        "## Results",
        "",
        "| Suite | Arm | Tasks | Verified | Success (95% Wilson CI) | Tokens / success "
        "| Tool calls / success | Mean latency ms | Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for suite in Suite:
        arms = report.suites.get(suite.value, {})
        for arm in Arm:
            m = arms.get(arm.value)
            if m is not None:
                label = suite.value + (" (correct refusals)" if suite == Suite.NEGATIVE else "")
                lines.append(_row(label, arm.value, m))
    fa = report.false_applicability_rate
    lb = report.success_lower_bound
    lines += [
        "",
        f"- False applicability: {_fmt(None if fa is None else fa * 100)}%; "
        f"regressions vs baseline: {report.regressions}; "
        f"95% lower bound on held-out+transfer success: {_fmt(None if lb is None else lb * 100)}%",
        "",
        "## Lifecycle cost (includes learning and evaluation overhead)",
        "",
        f"- Learning: {lc.learning_tokens} tokens, {lc.learning_wall_ms} ms wall time",
        f"- Evaluation overhead (control arms A+B): {lc.evaluation_overhead_tokens} tokens",
        f"- Capability execution: {lc.capability_exec_tokens} tokens for "
        f"{lc.capability_verified} verified successes",
        f"- **Lifecycle tokens per verified success (C): {_fmt(lc.lifecycle_tokens_per_success)}** "
        f"vs baseline execution only: {_fmt(lc.baseline_tokens_per_success)}",
        "",
        "## Notes",
        "",
        "- Source replay is necessary but never sufficient; held-out, transfer and "
        "negative-applicability evidence is required for a PASS.",
        "- Success is decided by the independent verifier from final environment state "
        "for every arm.",
        "- Lifecycle cost charges the capability for the control-arm tokens spent evaluating it; "
        "amortization over future use is not credited here.",
        "- Small samples: treat differences within the confidence intervals as unproven.",
        "",
    ]
    return "\n".join(lines)
