"""Retrospective policy simulation (pure): re-decide recorded evaluations under another policy.

Inputs are what survived the M5.8 scratch database: the per-asset evaluation reports (aggregates)
and the task-level rows of the P2 comparison. This is policy-development evidence, never
independent confirmation, and it never promotes anything. Where the data cannot support a gate
(task-level clustering of validation runs, variants, terminations) the simulation says so and
reports a conservative and an optimistic scenario instead of inventing numbers.
"""

import json
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from experience_engine.domain.evaluation import (
    Arm,
    ArmMetrics,
    PromotionPolicy,
    RiskBasedPolicy,
    RiskEvidence,
    Suite,
    TaskResult,
    Verdict,
    default_policies,
    default_policies_v2,
)
from experience_engine.domain.stats import wilson_interval
from experience_engine.evaluation.policy import decide_verdict
from experience_engine.evaluation.risk import build_risk_evidence, cp_upper
from experience_engine.evaluation.risk_policy import (
    decide_verdict_v2,
    noninferiority_gate,
    regression_gate,
    variant_gates,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SuiteAggregate(_Frozen):
    runs: int
    tasks: int | None
    verified: int


class AssetAggregate(_Frozen):
    asset: str
    kind: str  # MEMORY | CAPABILITY
    treatment_arm: str
    recorded_verdict: str
    recorded_reasons: list[str]
    seeds: int
    suites: dict[str, SuiteAggregate]  # treatment arm per suite
    baseline: dict[str, SuiteAggregate]
    regression_pairs: int
    regressions: int
    false_applicability: float
    success_lower_bound: float


_ROW = re.compile(
    r"^\| (SOURCE_REPLAY|HELD_OUT|TRANSFER|NEGATIVE[^|]*|REGRESSION) \| (\S+) \| (\d+)"
    r"(?: \((\d+) tasks\))? \| (\d+) \|"
)


def parse_validation_report(text: str, asset: str) -> AssetAggregate:
    verdict = re.search(r"\*\*Verdict: (\w+)\*\*", text)
    assert verdict, "no verdict line"
    block = text.split("Reasons:")[1].split("\n\n")[0]
    reasons = [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ")]
    seeds = re.search(r"seeds \[([^\]]*)\]", text)
    treat: dict[str, SuiteAggregate] = {}
    base: dict[str, SuiteAggregate] = {}
    arm_name = ""
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        suite, arm, runs, tasks, ver = m.groups()
        suite = "NEGATIVE" if suite.startswith("NEGATIVE") else suite
        agg = SuiteAggregate(runs=int(runs), tasks=int(tasks) if tasks else None, verified=int(ver))
        if arm == "A_BASELINE":
            base[suite] = agg
        elif arm != "B_MEMORY":
            treat[suite] = agg
            arm_name = arm
    fa = re.search(r"False applicability: (\d+)%; regressions vs baseline: (\d+); .* (\d+)%", text)
    assert fa, "no summary line"
    reg = treat["REGRESSION"]
    return AssetAggregate(
        asset=asset,
        kind="MEMORY" if arm_name == "M_MEMORY_ASSET" else "CAPABILITY",
        treatment_arm=arm_name,
        recorded_verdict=verdict.group(1),
        recorded_reasons=reasons,
        seeds=len([s for s in (seeds.group(1) if seeds else "0").split(",") if s.strip()]),
        suites={k: v for k, v in treat.items() if k != "REGRESSION"},
        baseline=base,
        regression_pairs=reg.runs,
        regressions=int(fa.group(2)),
        false_applicability=int(fa.group(1)) / 100,
        success_lower_bound=int(fa.group(3)) / 100,
    )


def _metrics(a: SuiteAggregate) -> ArmMetrics:
    lo, hi = wilson_interval(a.verified, a.runs)
    return ArmMetrics(
        n=a.runs,
        n_tasks=a.tasks,
        verified=a.verified,
        rate=a.verified / a.runs if a.runs else 0.0,
        ci_low=lo,
        ci_high=hi,
        tokens_total=0,
        tool_calls_total=0,
        latency_mean_ms=0.0,
        tokens_per_success=None,
        tool_calls_per_success=None,
    )


def suites_of(agg: AssetAggregate) -> dict[str, dict[str, ArmMetrics]]:
    arm = agg.treatment_arm
    out: dict[str, dict[str, ArmMetrics]] = {s: {arm: _metrics(v)} for s, v in agg.suites.items()}
    for s, v in agg.baseline.items():
        out.setdefault(s, {})[Arm.BASELINE.value] = _metrics(v)
    return out


def false_applicability_of(agg: AssetAggregate) -> float:
    return agg.false_applicability


def replay_v1(
    agg: AssetAggregate, policy: PromotionPolicy | None = None
) -> tuple[Verdict, list[str]]:
    """The existing count-based policy on the recorded aggregates (reproduces M5.8)."""
    pol = policy or default_policies()["low"]
    return decide_verdict(
        pol,
        suites_of(agg),
        false_applicability_rate=agg.false_applicability,
        regressions=agg.regressions,
        treatment=Arm(agg.treatment_arm),
    )


def evidence_scenarios(agg: AssetAggregate) -> dict[str, RiskEvidence]:
    """Regression evidence from aggregates under two bracketing assumptions.

    `pairs` treats every baseline-solved (task, repetition) as independent (optimistic: repeats
    are correlated). `tasks` divides counts by the number of repetitions, i.e. one effective
    observation per distinct task (conservative: assumes a task regresses in all or none of its
    repetitions)."""
    seeds = max(1, agg.seeds)
    src = agg.suites.get("SOURCE_REPLAY")
    common = {
        "n_pairs": agg.regression_pairs,
        "n_regressions": agg.regressions,
        "n_dropped_unknown": 0,
        "improvement_tasks": 0,
        "variant_pairs": {},
        "paired_diffs": [],
        "source_tasks": (src.tasks or 0) if src else 0,
        "source_mass": ((src.verified / src.runs) * (src.tasks or 0)) if src and src.runs else 0.0,
        "termination_counts": {},
    }
    return {
        "pairs": RiskEvidence(
            regression_tasks=agg.regression_pairs, regression_mass=float(agg.regressions), **common
        ),
        "tasks": RiskEvidence(
            regression_tasks=math.ceil(agg.regression_pairs / seeds),
            regression_mass=agg.regressions / seeds,
            **common,
        ),
    }


def replay_v2(
    agg: AssetAggregate, scenario: str, policy: RiskBasedPolicy | None = None
) -> tuple[Verdict, list[str]]:
    """Risk-based policy on aggregates. G2 (paired task-level non-inferiority) and G7 (variants)
    cannot be evaluated without task-level rows and are skipped here (see P2 task-level view)."""
    pol = policy or default_policies_v2()["low"]
    return decide_verdict_v2(
        pol,
        suites_of(agg),
        false_applicability_rate=agg.false_applicability,
        evidence=evidence_scenarios(agg)[scenario],
        treatment=Arm(agg.treatment_arm),
        require_paired=False,
    )


def p2_task_evidence(rows: Sequence[dict[str, object]], asset_ref: str, arm: str) -> RiskEvidence:
    """Task-level paired evidence for one asset from the P2 comparison rows (one repetition)."""
    base: dict[tuple[str, int], dict[str, object]] = {}
    treat: dict[tuple[str, int], dict[str, object]] = {}
    for r in rows:
        key = (str(r["task_id"]), int(r["rep"]))  # type: ignore[call-overload]
        if r["arm"] == "A":
            base[key] = r
        elif r["arm"] == arm and r.get("asset_ref") == asset_ref:
            treat[key] = r
    out: list[TaskResult] = []

    def row(r: dict[str, object], a: Arm) -> TaskResult:
        suite = Suite.HELD_OUT if r["split"] == "VALIDATION" else Suite.TRANSFER
        return TaskResult(
            suite=suite,
            arm=a,
            task_id=str(r["task_id"]),
            family=str(r["family"]),
            variant=str(r["variant"]),
            status=str(r["status"]),
            tokens=int(r["tokens"]),  # type: ignore[call-overload]
            tool_calls=int(r["tool_calls"]),  # type: ignore[call-overload]
            latency_ms=int(r["latency_ms"]),  # type: ignore[call-overload]
            termination=str(r.get("termination") or ""),
            seed=int(r["rep"]),  # type: ignore[call-overload]
        )

    for key, t in treat.items():
        b = base.get(key)
        if b is None or t["family"] != b["family"]:
            continue
        out.append(row(b, Arm.BASELINE))
        out.append(row(t, Arm.MEMORY_ASSET))
    return build_risk_evidence(out, Arm.MEMORY_ASSET.value)


def p2_gate_summary(
    evidence: RiskEvidence, policy: RiskBasedPolicy | None = None
) -> dict[str, str]:
    pol = policy or default_policies_v2()["low"]
    gates = {
        "G1 regression": regression_gate(pol, evidence),
        "G2 non-inferiority": noninferiority_gate(pol, evidence),
    }
    out = {k: f"{v[0].upper()}: {v[1]}" for k, v in gates.items()}
    for i, (status, text) in enumerate(variant_gates(pol, evidence)):
        out[f"G7 variant {i + 1}"] = f"{status.upper()}: {text}"
    return out


def load_aggregates(path: Path) -> list[AssetAggregate]:
    return [AssetAggregate.model_validate(x) for x in json.loads(path.read_text())]


def aggregates_from_dir(directory: Path) -> list[AssetAggregate]:
    names = {
        "cap-pool": "pool.f12f4fa920@1",
        "cap-timeout": "timeout.e2fb15fa79@1",
    }
    out = []
    for f in sorted(directory.glob("*.md")):
        stem = f.stem
        asset = names.get(stem) or (stem[:-3] + "@1" if stem.endswith("-v1") else stem)
        out.append(parse_validation_report(f.read_text(), asset))
    return out


def cp_ucb_pairs(agg: AssetAggregate, confidence: float = 0.95) -> float:
    return cp_upper(agg.regressions, agg.regression_pairs, confidence)


def tabulate_decisions(
    aggs: Sequence[AssetAggregate],
) -> dict[str, dict[str, str]]:
    """asset -> {policy label: verdict}, the table Phase 3 reports."""
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for a in aggs:
        out[a.asset]["default-low"] = replay_v1(a)[0].value
        out[a.asset]["v2 (pairs)"] = replay_v2(a, "pairs")[0].value
        out[a.asset]["v2 (tasks)"] = replay_v2(a, "tasks")[0].value
    return dict(out)


_P2_ARM_OF_ASSET = {"mem_episodic": "B", "mem_consolidated": "C", "mem_procedural": "D"}


def _p2_arm(asset: str) -> str:
    for prefix, arm in _P2_ARM_OF_ASSET.items():
        if asset.startswith(prefix):
            return arm
    return "E"


def render_retrospective(
    aggs: Sequence[AssetAggregate], p2_rows: Sequence[dict[str, object]], title: str
) -> str:
    v1p, v2p = default_policies()["low"], default_policies_v2()["low"]
    lines = [
        f"# {title}",
        "",
        "Generated by `python -m experience_engine.evaluation.policy_simulation`. "
        "**Policy-development "
        "evidence, not independent confirmation; nothing here promotes an asset.** Policies: "
        f"`{v1p.name}` (hash `{v1p.policy_hash()[:12]}…`) and `{v2p.name}` "
        f"(hash `{v2p.policy_hash()[:12]}…`). Design: "
        "[promotion-policy-v2-design.md](../experiments/promotion-policy-v2-design.md).",
        "",
        "## 1. Reproduction of M5.8 under the existing policy",
        "",
        "The recorded aggregates are re-decided by the unchanged `decide_verdict`; every recorded "
        "verdict **and reason list** must be reproduced exactly.",
        "",
        "| Asset | recorded verdict | replayed verdict | reasons identical |",
        "|---|---|---|---|",
    ]
    for a in aggs:
        v, r = replay_v1(a)
        lines.append(
            f"| `{a.asset}` | {a.recorded_verdict} | {v.value} | "
            f"{'yes' if r == a.recorded_reasons else 'NO'} |"
        )
    lines += [
        "",
        "## 2. Decisions on the P1 validation aggregates",
        "",
        "Two bracketing scenarios for the unknown clustering of the validation runs: **pairs** "
        "(every baseline-solved run independent; optimistic) and **tasks** (one effective "
        "observation per distinct task; conservative). G2 (paired non-inferiority) and G7 "
        "(variants) "
        "need task-level rows that no longer exist for P1 and are **not evaluated** here (see §4).",
        "",
        "| Asset | kind | existing | candidate (pairs) | candidate (tasks) |",
        "|---|---|---|---|---|",
    ]
    for a in aggs:
        lines.append(
            f"| `{a.asset}` | {a.kind.lower()} | {replay_v1(a)[0].value} | "
            f"{replay_v2(a, 'pairs')[0].value} | {replay_v2(a, 'tasks')[0].value} |"
        )
    tot = {"default-low": [0, 0, 0], "pairs": [0, 0, 0], "tasks": [0, 0, 0]}
    for a in aggs:
        for key, v in (
            ("default-low", replay_v1(a)[0]),
            ("pairs", replay_v2(a, "pairs")[0]),
            ("tasks", replay_v2(a, "tasks")[0]),
        ):
            tot[key][("PASS", "FAIL", "INCONCLUSIVE").index(v.value)] += 1
    lines += [
        "",
        "| Metric | existing `default-low` | candidate (pairs) | candidate (tasks) |",
        "|---|---|---|---|",
        f"| PASS decisions | {tot['default-low'][0]} | {tot['pairs'][0]} | {tot['tasks'][0]} |",
        f"| REJECT decisions (FAIL) | {tot['default-low'][1]} | {tot['pairs'][1]} | "
        f"{tot['tasks'][1]} |",
        f"| INCONCLUSIVE (more evidence needed) | {tot['default-low'][2]} | {tot['pairs'][2]} | "
        f"{tot['tasks'][2]} |",
        "",
        "### Gate detail per asset",
        "",
        "| Asset | regressions / baseline-solved runs | rate | 95% UCB (pairs) | 95% UCB (tasks) | "
        "source replay | held-out | transfer | false applicability | existing reasons |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in aggs:
        ev = evidence_scenarios(a)
        ucb_p = cp_upper(ev["pairs"].regression_mass, ev["pairs"].regression_tasks, 0.95)
        ucb_t = cp_upper(ev["tasks"].regression_mass, ev["tasks"].regression_tasks, 0.95)

        def rate(s: str, a: AssetAggregate = a) -> str:
            x = a.suites[s]
            return f"{x.verified}/{x.runs}"

        lines.append(
            f"| `{a.asset}` | {a.regressions}/{a.regression_pairs} | "
            f"{a.regressions / a.regression_pairs:.0%} | {ucb_p:.0%} | {ucb_t:.0%} | "
            f"{rate('SOURCE_REPLAY')} | {rate('HELD_OUT')} | {rate('TRANSFER')} | "
            f"{a.false_applicability:.0%} | {'; '.join(a.recorded_reasons)} |"
        )
    lines += [
        "",
        "Candidate verdict reasons (pairs scenario unless it differs):",
        "",
    ]
    for a in aggs:
        for sc in ("pairs", "tasks"):
            v, r = replay_v2(a, sc)
            lines.append(f"* `{a.asset}` [{sc}] {v.value}: {'; '.join(r)}")
    lines += [
        "",
        "## 3. Classification",
        "",
        "* **Legitimate quality problem (rejected by both policies in both scenarios):** "
        + ", ".join(
            f"`{a.asset}`"
            for a in aggs
            if replay_v1(a)[0] == Verdict.FAIL
            and replay_v2(a, "pairs")[0] == Verdict.FAIL
            and replay_v2(a, "tasks")[0] == Verdict.FAIL
        )
        + ".",
        "* **Rejected primarily by the absolute regression limit (existing FAIL, candidate does "
        "not FAIL in the pairs scenario):** "
        + ", ".join(
            f"`{a.asset}`"
            for a in aggs
            if replay_v1(a)[0] == Verdict.FAIL and replay_v2(a, "pairs")[0] != Verdict.FAIL
        )
        + ".",
        "* **Would pass the candidate policy in both scenarios (still not promoted):** "
        + ", ".join(
            f"`{a.asset}`"
            for a in aggs
            if replay_v2(a, "pairs")[0] == Verdict.PASS and replay_v2(a, "tasks")[0] == Verdict.PASS
        )
        + ".",
        "* **Evidence remains insufficient (INCONCLUSIVE in at least one scenario):** "
        + ", ".join(
            f"`{a.asset}`"
            for a in aggs
            if Verdict.INCONCLUSIVE in (replay_v2(a, "pairs")[0], replay_v2(a, "tasks")[0])
        )
        + ".",
        "",
        "## 4. Task-level view on the independent P2 comparison (G1, G2, G7)",
        "",
        "P2 task-level rows (one repetition per task, assets forced ACTIVE in an experimental run; "
        "baseline = arm A on the same task). Source replay, false applicability and the suite "
        "structure do not exist in these rows, so only the task-level gates are shown.",
        "",
        "| Asset | baseline-solved tasks | regressed | improved tasks | G1 | G2 | G7 |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in aggs:
        pe = p2_task_evidence(p2_rows, a.asset, _p2_arm(a.asset))
        g = p2_gate_summary(pe)
        g7 = "; ".join(v for k, v in g.items() if k.startswith("G7")) or "no variant flagged"
        lines.append(
            f"| `{a.asset}` | {pe.regression_tasks} | {pe.n_regressions} | "
            f"{pe.improvement_tasks} | "
            f"{g['G1 regression']} | {g['G2 non-inferiority']} | {g7} |"
        )
    lines += [
        "",
        "## 5. Limits",
        "",
        "* The same data motivated the policy design (audit), so agreement here is not evidence "
        "that the "
        "policy generalises; Phase 5 uses fresh seeds.",
        "* Validation task-level rows were lost with the M5.8 scratch database; bounds there are "
        "bracketed, not exact. G2, G7 and critical-violation counts could not be evaluated on P1.",
        "* P2 shows one repetition per task and shares the noisy-baseline property of the "
        "regression estimate.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="policy-simulation")
    ap.add_argument("--reports", default="docs/evaluations/gated-validation")
    ap.add_argument(
        "--p2-results",
        default="docs/evaluations/data/gated-scoped-unvalidated-gemini-2.5-flash.json",
    )
    ap.add_argument(
        "--aggregates-out", default="docs/evaluations/data/m58-validation-aggregates.json"
    )
    ap.add_argument("--out", default="docs/evaluations/promotion-policy-retrospective.md")
    args = ap.parse_args(argv)
    aggs = aggregates_from_dir(Path(args.reports))
    Path(args.aggregates_out).write_text(
        json.dumps([a.model_dump(mode="json") for a in aggs], indent=1, sort_keys=True) + "\n"
    )
    rows = json.loads(Path(args.p2_results).read_text())["rows"]
    Path(args.out).write_text(
        render_retrospective(aggs, rows, "M5.9 retrospective policy simulation on M5.8 data")
    )
    print(f"wrote {args.out} and {args.aggregates_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
