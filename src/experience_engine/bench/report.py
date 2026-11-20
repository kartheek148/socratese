import math
import os
import re
from dataclasses import dataclass

from experience_engine.domain.benchmark import BenchmarkRunRecord
from experience_engine.domain.outcome import OutcomeStatus

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}"),
]
_SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Summary:
    n: int
    verified: int
    statuses: dict[str, int]
    false_claims: int
    tokens_total: int
    tool_calls_total: int
    latency_mean_ms: float
    human_interventions: int

    @property
    def rate(self) -> float:
        return self.verified / self.n if self.n else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        return wilson_interval(self.verified, self.n)

    @property
    def tokens_per_success(self) -> float | None:
        return self.tokens_total / self.verified if self.verified else None

    @property
    def tool_calls_per_success(self) -> float | None:
        return self.tool_calls_total / self.verified if self.verified else None


def summarize(records: list[BenchmarkRunRecord]) -> Summary:
    statuses = {s.value: 0 for s in OutcomeStatus}
    for r in records:
        statuses[r.outcome.status.value] += 1
    n = len(records)
    return Summary(
        n=n,
        verified=statuses[OutcomeStatus.VERIFIED_SUCCESS.value],
        statuses=statuses,
        # Agent said it was done, but the independent verifier did not confirm success.
        false_claims=sum(
            1
            for r in records
            if r.agent_claimed_done and r.outcome.status != OutcomeStatus.VERIFIED_SUCCESS
        ),
        tokens_total=sum(r.outcome.tokens_used for r in records),
        tool_calls_total=sum(r.outcome.tool_calls for r in records),
        latency_mean_ms=sum(r.outcome.execution_time_ms for r in records) / n if n else 0.0,
        human_interventions=sum(r.outcome.human_interventions for r in records),
    )


def _fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _row(label: str, s: Summary) -> str:
    lo, hi = s.ci
    return (
        f"| {label} | {s.n} | {s.verified} | {s.rate:.0%} ({lo:.0%}-{hi:.0%}) | "
        f"{s.statuses['FAILURE']}/{s.statuses['PARTIAL']}/{s.statuses['UNKNOWN']} | "
        f"{s.false_claims} | {_fmt(s.tokens_per_success, 0)} | {_fmt(s.tool_calls_per_success)} | "
        f"{s.latency_mean_ms:.0f} | {s.human_interventions} |"
    )


def _config_line(first: BenchmarkRunRecord) -> str:
    if first.config.config_id == "A_BASELINE":
        return (
            "Configuration **A (fixed baseline, no persistent learning, no memory, "
            "no capabilities)**."
        )
    return (
        f"Configuration **{first.config.config_id}** (runtime capability matching; "
        "see `docs/runtime.md`). Not the no-learning baseline."
    )


def render_report(records: list[BenchmarkRunRecord], *, title: str = "Baseline report") -> str:
    if not records:
        raise ValueError("no records to report")
    first = records[0]
    by_split: dict[str, list[BenchmarkRunRecord]] = {}
    by_variant: dict[tuple[str, str], list[BenchmarkRunRecord]] = {}
    for r in records:
        by_split.setdefault(r.dataset.split.value, []).append(r)
        by_variant.setdefault((r.dataset.split.value, r.tags.get("variant", "?")), []).append(r)

    header = (
        "| Group | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | "
        "False claims | Tokens / verified success | Tool calls / verified success | "
        "Mean latency ms | Human interventions |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    model = first.config.model
    lines = [
        f"# {title}",
        "",
        _config_line(first),
        "",
        "## Reproducibility metadata",
        "",
        f"- Dataset: `{first.dataset.name}` v{first.dataset.version}",
        *(
            f"  - {split}: content hash `{rs[0].dataset.content_hash[:16]}…` "
            f"({rs[0].dataset.n_tasks} tasks)"
            for split, rs in sorted(by_split.items())
        ),
        f"- Seed: {first.seed}",
        f"- Provider / model: `{model.provider}` / `{model.model}` "
        f"(temperature {model.temperature}, seed {model.seed}, "
        f"max output tokens {model.max_output_tokens})",
        f"- Prompt version: `{first.config.prompt_version}`; runtime `{first.runtime_version}`; "
        f"verifier `{first.verifier_version}`; environment `{first.environment_version}`; "
        f"policy `{first.decision.policy_version}`",
        "- Learning cost: 0 (this run performs no learning). Lifecycle cost equals execution "
        "cost; the cost of producing any capability used is reported separately.",
        "- Run-record digests (deterministic fields only; IDs, timestamps and latency excluded) "
        "make two runs comparable via `ee-bench compare`.",
        "",
    ]
    if model.provider == "fake":
        lines += [
            "> **Caution:** this report was produced with the deterministic scripted `FakeLLM`. It "
            "validates the harness and verifier; it says **nothing** about real-model capability. "
            "Token counts are whitespace estimates.",
            "",
        ]
    lines += ["## Results by split", "", header]
    lines += [_row(split, summarize(rs)) for split, rs in sorted(by_split.items())]
    lines += ["", "## Results by split and variant", "", header]
    lines += [
        _row(f"{split} / {variant}", summarize(rs))
        for (split, variant), rs in sorted(by_variant.items())
    ]
    lines += [
        "",
        "## Notes and limitations",
        "",
        "- Success is decided only by the independent verifier from final environment state; "
        "`False claims` counts runs where the agent declared completion but the verifier "
        "did not confirm.",
        "- UNKNOWN (e.g. provider outage) is reported separately and never counted as "
        "success or failure evidence.",
        "- Samples are small (see CIs); differences between configurations need more tasks/seeds "
        "before being treated as improvements.",
        "- The hidden-transfer split is protected only by an application-level access guard in M1.",
        "",
    ]
    text = "\n".join(lines)
    assert_no_secrets(text)
    return text


def assert_no_secrets(text: str) -> None:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"report contains secret-like content matching {pattern.pattern}")
    for name, value in os.environ.items():
        if len(value) >= 8 and any(h in name.upper() for h in _SECRET_ENV_HINTS) and value in text:
            raise ValueError(f"report contains the value of environment variable {name}")


@dataclass(frozen=True)
class Comparison:
    identical: bool
    only_in_a: list[str]
    only_in_b: list[str]
    changed: list[str]
    summary_a: Summary
    summary_b: Summary


def compare_runs(a: list[BenchmarkRunRecord], b: list[BenchmarkRunRecord]) -> Comparison:
    da = {r.tags["task_id"]: r.digest() for r in a}
    db = {r.tags["task_id"]: r.digest() for r in b}
    changed = sorted(k for k in da.keys() & db.keys() if da[k] != db[k])
    only_a, only_b = sorted(da.keys() - db.keys()), sorted(db.keys() - da.keys())
    return Comparison(
        identical=not (changed or only_a or only_b),
        only_in_a=only_a,
        only_in_b=only_b,
        changed=changed,
        summary_a=summarize(a),
        summary_b=summarize(b),
    )


def _strategy_counts(records: list[BenchmarkRunRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        key = r.decision.selected_action.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def render_comparison(
    baseline: list[BenchmarkRunRecord],
    candidate: list[BenchmarkRunRecord],
    *,
    title: str = "Capability runtime comparison",
) -> str:
    """Paired comparison of arm A (baseline) and arm C (capability runtime) on the same tasks."""
    if not baseline or not candidate:
        raise ValueError("both arms need records")
    a_by = {r.tags["task_id"]: r for r in baseline}
    c_by = {r.tags["task_id"]: r for r in candidate}
    shared = sorted(a_by.keys() & c_by.keys())
    if not shared:
        raise ValueError("the two arms share no tasks")
    a = [a_by[k] for k in shared]
    c = [c_by[k] for k in shared]
    sa, sc = summarize(a), summarize(c)
    experimental = any(r.tags.get("capability_catalog") == "experimental" for r in c)
    used = [r for r in c if r.outcome.capability_version]
    fallbacks = [r for r in c if "capability_fallback" in r.tags]
    versions: dict[str, int] = {}
    for r in used:
        versions[r.outcome.capability_version or ""] = (
            versions.get(r.outcome.capability_version or "", 0) + 1
        )

    def ok(r: BenchmarkRunRecord) -> bool:
        return r.outcome.status == OutcomeStatus.VERIFIED_SUCCESS

    both = sum(ok(x) and ok(y) for x, y in zip(a, c, strict=True))
    only_c = sum(ok(y) and not ok(x) for x, y in zip(a, c, strict=True))
    only_a = sum(ok(x) and not ok(y) for x, y in zip(a, c, strict=True))
    neither = len(a) - both - only_c - only_a
    (alo, ahi), (clo, chi) = sa.ci, sc.ci
    overlap = alo <= chi and clo <= ahi

    header = (
        "| Arm | Runs | Verified | Verified success (95% Wilson CI) | Fail/Partial/Unknown | "
        "False claims | Tokens / verified success | Tool calls / verified success | "
        "Mean latency ms | Human interventions |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    first = c[0]
    lines = [f"# {title}", ""]
    if experimental:
        lines += [
            "> **EXPERIMENTAL CATALOG.** Arm C used capabilities that were **not promoted** to "
            "ACTIVE by the evaluation workflow. This measures the runtime mechanism only; it is "
            "not evidence that any capability is approved, and must not be used to promote one.",
            "",
        ]
    if first.config.model.provider == "fake":
        lines += [
            "> **Caution:** scripted `FakeLLM`; validates mechanics, not real-model value.",
            "",
        ]
    lines += [
        "## Setup",
        "",
        f"- Paired tasks: {len(shared)} (identical task and environment per arm; every run in a "
        "fresh session; arm C sees only capabilities in the catalog, no transcripts).",
        f"- Arm A: `{a[0].config.config_id}`, policy `{a[0].decision.policy_version}`.",
        f"- Arm C: `{first.config.config_id}`, policy `{first.decision.policy_version}`.",
        f"- Model: `{first.config.model.provider}` / `{first.config.model.model}`.",
        f"- Dataset: `{first.dataset.name}` v{first.dataset.version}, seed {first.seed}.",
        "",
        "## Outcomes",
        "",
        header,
        _row("A baseline", sa),
        _row("C capability runtime", sc),
        "",
        f"Paired: both succeed {both}, only C {only_c}, only A {only_a}, neither {neither}.",
        "",
        "Confidence intervals "
        + (
            "**overlap**: the difference is not statistically distinguishable at this sample size."
            if overlap
            else "**do not overlap**: the difference is unlikely to be chance."
        ),
        "",
        "## Capability usage (arm C)",
        "",
        f"- Strategies selected: {_strategy_counts(c)}",
        f"- Runs that used a capability: {len(used)}/{len(c)}; versions: {versions or 'none'}",
        f"- Fell back to general planning: {len(fallbacks)}",
        f"- Capability aborts: "
        f"{sum(r.termination.value == 'CAPABILITY_ABORTED' for r in c)}; "
        f"escalations: {sum(r.termination.value == 'ESCALATED' for r in c)}",
        "",
        "## Notes",
        "",
        "- Success is decided only by the independent verifier from final environment state.",
        "- Tokens and tool calls include capability-internal decisions and any fallback work.",
        "- The cost of learning and evaluating the capability is **not** in this table; see the "
        "evaluation report for it. Lifecycle cost must be amortised over expected reuse.",
        "- Differences need more tasks/seeds before being treated as improvements.",
        "",
    ]
    text = "\n".join(lines)
    assert_no_secrets(text)
    return text
