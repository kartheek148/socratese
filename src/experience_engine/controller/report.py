"""Markdown report of the controller's prequential full-information evaluation (generated)."""

# ruff: noqa: E501  (table headers and report prose)

from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.controller.datasets import describe
from experience_engine.controller.simulate import ArmSummary, Comparison, SimConfig, TaskOutcome


@dataclass(frozen=True)
class SimEntry:
    kind: str
    config: SimConfig
    comparison: Comparison


@dataclass(frozen=True)
class SimSet:
    name: str
    n_tasks: int
    note: str
    tasks: Sequence[TaskOutcome]
    entries: Sequence[SimEntry]


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _tok(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.0f}"


def _fmt(v: float, pct: bool) -> str:
    return f"{v:+.1%}" if pct else f"{v:.3f}"


def _ci(t: tuple[float | None, float | None, float | None], pct: bool = True) -> str:
    p, lo, hi = t
    if p is None or lo is None or hi is None:
        return "n/a"
    return f"{_fmt(p, pct)} [{_fmt(lo, pct)}, {_fmt(hi, pct)}]"


def _arm(a: ArmSummary) -> str:
    return f"{_pct(a.success_rate)} / {_tok(a.tokens_per_success)}"


def render_sim_report(sets: Sequence[SimSet], title: str, preamble: str = "") -> str:
    out = [f"# {title}", "", preamble.strip(), ""]
    for s in sets:
        out += [f"## {s.name}", "", f"{s.note}", ""]
        out += ["### Recorded per-action truth (full information)", ""]
        out += ["| family | action | tasks | success | mean tokens | tokens per verified success |"]
        out += ["|---|---|---|---|---|---|"]
        for fam, acts in describe(s.tasks).items():
            for key, (n, rate, tok) in acts.items():
                cps = tok / rate if rate > 0 else None
                out.append(f"| {fam} | `{key}` | {n} | {_pct(rate)} | {_tok(tok)} | {_tok(cps)} |")
        out += [
            "",
            "### Controller versus the rule baseline (per-task paired, cluster bootstrap)",
            "",
        ]
        out += [
            "Cells are `success / tokens per verified success`. The unit of inference is the "
            "task (orders are repeated draws of the same tasks); one recorded draw per task and action.",
            "",
        ]
        out += [
            "| controller | verdict | controller | rule baseline | success diff [95% one-sided bounds] "
            "| cost ratio [bounds] | deviates | explores | exploration: successes lost / tokens vs default (per order) | protected deviations |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for e in s.entries:
            c = e.comparison
            out.append(
                f"| {e.kind} | **{c.verdict.value}** | {_arm(c.controller)} | {_arm(c.baseline)} | "
                f"{_ci(c.success_diff)} | {_ci(c.cost_ratio, pct=False)} | {c.deviation_rate:.1%} | "
                f"{c.exploration_rate:.1%} | {c.exploration_successes_lost_per_order:.1f} / "
                f"{c.exploration_tokens_delta_per_order:+,.0f} | {c.protected_deviations} |"
            )
        out += [
            "",
            "Per family (controller vs rule baseline, `success / tokens per verified success`, deviation rate):",
            "",
        ]
        out += [
            "| controller | family | controller | rule baseline | deviates |",
            "|---|---|---|---|---|",
        ]
        for e in s.entries:
            for fam, (ca, ba, dev) in e.comparison.by_family.items():
                out.append(f"| {e.kind} | {fam} | {_arm(ca)} | {_arm(ba)} | {dev:.1%} |")
        out.append("")
        if any(e.comparison.family_results for e in s.entries):
            out += [
                "Per-family verdicts (per-task cluster bootstrap, one-sided 95%). The cell verdict uses the "
                "controller's contractual 10-point non-inferiority margin; the A2 verdict uses the M6 "
                "5-point margin (under-powered at these sizes).",
                "",
                "| controller | family | tasks | success diff [bounds] | cost ratio [bounds] | cell verdict (10 pt) | A2 verdict (5 pt) | deviates | explores | exploration: successes lost / tokens vs default (per order) |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for e in s.entries:
                for fam, fr in e.comparison.family_results.items():
                    out.append(
                        f"| {e.kind} | {fam} | {fr.n_tasks} | {_ci(fr.success_diff)} | "
                        f"{_ci(fr.cost_ratio, pct=False)} | **{fr.verdict_cell.value}** | "
                        f"{fr.verdict_a2.value} | {fr.deviation_rate:.1%} | "
                        f"{fr.exploration_rate:.1%} | {fr.exploration_successes_lost_per_order:.1f} / "
                        f"{fr.exploration_tokens_delta_per_order:+,.0f} |"
                    )
            out.append("")
    return "\n".join(out) + "\n"
