"""Markdown for the production-aligned evaluation (generated; every number comes from the rows).

E1 (capability phase only) is derived from the E rows' attribution, never executed separately, so
it is paired with E (production runtime with fallback) by construction.
"""
# ruff: noqa: E501

from collections import Counter
from collections.abc import Sequence

from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.experiment import ExperimentResult, RunRow
from experience_engine.domain.serving import AssetServingEvaluation, ServingPolicy, serving_low_v1
from experience_engine.evaluation import paired
from experience_engine.evaluation import task_stats as ts
from experience_engine.evaluation.serving_policy import evaluate_assets

ARM_LABEL = {
    "A": "A stateless baseline",
    "B": "B episodic memory",
    "C": "C consolidated memory",
    "D": "D procedural memory",
    "E1": "E1 capability only (derived)",
    "E": "E2 capability + production fallback",
    "F": "F empty-registry runtime",
    "UB": "UB ungated episodic",
    "UC": "UC ungated consolidated",
    "UD": "UD ungated instructions",
}


def derive_e1_rows(rows: Sequence[RunRow]) -> list[RunRow]:
    """Capability phase of each production run where a capability was attempted."""
    out = []
    for r in rows:
        s = r.strategy
        if r.arm != "E" or s is None or not s.attempted or s.capability is None:
            continue
        cap = s.capability
        known = cap.verified is not None
        out.append(
            r.model_copy(
                update={
                    "arm": "E1",
                    "status": cap.outcome if known and cap.outcome else "UNKNOWN",
                    "verified": bool(cap.verified),
                    "tokens": cap.tokens,
                    "tool_calls": cap.tool_calls,
                    "latency_ms": cap.latency_ms,
                    "fell_back": None,
                    "strategy": None,
                    "termination": cap.reason,
                }
            )
        )
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _rate_by_task(
    rows: Sequence[RunRow], arm: str, tasks: set[str] | None = None
) -> dict[str, float]:
    r = paired.task_rates([x for x in rows if tasks is None or x.task_id in tasks], arm)
    return r


def _family_table(rows: Sequence[RunRow], arms: Sequence[str]) -> list[str]:
    fams = sorted({r.family for r in rows if r.arm == "A"})
    lines = [
        "| Family | tasks | " + " | ".join(ARM_LABEL[a] for a in arms) + " |",
        "|---|---|" + "---|" * len(arms),
    ]
    for scope, tasks in [
        ("all", None),
        *[(f, {r.task_id for r in rows if r.arm == "A" and r.family == f}) for f in fams],
    ]:
        cells = []
        for a in arms:
            rt = _rate_by_task(rows, a, tasks)
            cells.append(f"{_pct(sum(rt.values()) / len(rt) if rt else None)} (n={len(rt)})")
        n = len(tasks) if tasks is not None else len({r.task_id for r in rows if r.arm == "A"})
        lines.append(f"| {scope} | {n} | " + " | ".join(cells) + " |")
    return lines


def _variant_table(rows: Sequence[RunRow], arms: Sequence[str]) -> list[str]:
    keys = sorted({(r.family, r.variant) for r in rows if r.arm == "A"})
    lines = [
        "| Variant | tasks | " + " | ".join(ARM_LABEL[a] for a in arms) + " |",
        "|---|---|" + "---|" * len(arms),
    ]
    for fam, var in keys:
        tasks = {r.task_id for r in rows if r.arm == "A" and r.family == fam and r.variant == var}
        cells = []
        for a in arms:
            rt = _rate_by_task(rows, a, tasks)
            cells.append(_pct(sum(rt.values()) / len(rt) if rt else None))
        lines.append(f"| {fam}/{var} | {len(tasks)} | " + " | ".join(cells) + " |")
    return lines


def _compare_table(rows: Sequence[RunRow], arms: Sequence[str]) -> list[str]:
    fams = sorted({r.family for r in rows if r.arm == "A"})
    scopes: list[tuple[str, set[str] | None]] = [("all", None)] + [
        (f, {r.task_id for r in rows if r.arm == "A" and r.family == f}) for f in fams
    ]
    comps = []
    for name, tasks in scopes:
        for a in arms:
            if a == "A":
                continue
            c = paired.compare(rows, a, "A", where=(lambda r, t=tasks: t is None or r.task_id in t))  # type: ignore[misc]
            comps.append((name, a, c))
    adj = paired.holm({f"{n}/{a}": c.p_mcnemar for n, a, c in comps})
    lines = [
        "| Scope | Arm vs A | tasks | rate arm | rate A | paired diff [95% cluster-bootstrap CI] | discordant arm-only/A-only | McNemar p (Holm) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for n, a, c in comps:
        lines.append(
            f"| {n} | {ARM_LABEL[a]} | {c.n_tasks} | {_pct(c.rate_x)} | {_pct(c.rate_y)} | {c.diff:+.1%} [{c.ci_lo:+.1%}, {c.ci_hi:+.1%}] | {c.only_x}/{c.only_y} | {c.p_mcnemar:.3f} ({adj[f'{n}/{a}']:.3f}) |"
        )
    return lines


def _stratified(rows: Sequence[RunRow], arms: Sequence[str], policy: ServingPolicy) -> list[str]:
    fams = sorted({r.family for r in rows if r.arm == "A"})
    lines = [
        "| Arm | stratum means (family: success) | overall, weighted by task counts [95% CI] | overall diff vs A [95% CI] |",
        "|---|---|---|---|",
    ]
    base = {
        f: _rate_by_task(rows, "A", {r.task_id for r in rows if r.arm == "A" and r.family == f})
        for f in fams
    }
    for a in arms:
        strata: dict[str, list[float]] = {}
        diffs: dict[str, list[float]] = {}
        for f in fams:
            tasks = {r.task_id for r in rows if r.arm == "A" and r.family == f}
            rt = _rate_by_task(rows, a, tasks)
            strata[f] = [rt[t] for t in sorted(rt)]
            diffs[f] = [rt[t] - base[f][t] for t in sorted(rt) if t in base[f]]
        if not any(strata.values()):
            continue
        est = ts.stratified_mean(
            strata, resamples=policy.bootstrap_resamples, seed=policy.bootstrap_seed
        )
        dest = (
            ts.stratified_mean(
                diffs, resamples=policy.bootstrap_resamples, seed=policy.bootstrap_seed
            )
            if all(diffs.values())
            else None
        )
        means = ", ".join(f"{k}: {v[1]:.0%} (n={v[0]})" for k, v in est.by_stratum.items())
        d = (
            f"{dest.overall:+.1%} [{dest.lo:+.1%}, {dest.hi:+.1%}]"
            if dest and dest.lo is not None and dest.hi is not None
            else "n/a"
        )
        lines.append(
            f"| {ARM_LABEL[a]} | {means} | {est.overall:.1%} [{est.lo:.1%}, {est.hi:.1%}] | {d} |"
        )
    return lines


def _cost_table(rows: Sequence[RunRow], arms: Sequence[str]) -> list[str]:
    lines = [
        "| Arm | runs | success | tokens/run | tokens per verified success | tool calls/run | latency s/run |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in arms:
        rs = [r for r in rows if r.arm == a and r.status != "UNKNOWN"]
        if not rs:
            continue
        ok = sum(r.verified for r in rs)
        tok = sum(r.tokens for r in rs)
        lines.append(
            f"| {ARM_LABEL[a]} | {len(rs)} | {ok / len(rs):.0%} | {tok / len(rs):,.0f} | {(tok / ok) if ok else float('nan'):,.0f} | {sum(r.tool_calls for r in rs) / len(rs):.1f} | {sum(r.latency_ms for r in rs) / len(rs) / 1000:.0f} |"
        )
    return lines


def _attribution(rows: Sequence[RunRow]) -> list[str]:
    caps = sorted({r.asset_ref for r in rows if r.arm == "E" and r.asset_ref})
    lines = [
        "| Capability | tasks attempted | E1 capability-only success | A on the same tasks | fallback frequency | recovery: final success after capability failed + fallback | A on those same tasks | E2 final success | capability tokens | fallback tokens | fallback share of tokens | fallback causes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ref in caps:
        er = [
            r
            for r in rows
            if r.arm == "E" and r.asset_ref == ref and r.strategy and r.strategy.attempted
        ]
        base = {(r.task_id, r.rep): r for r in rows if r.arm == "A"}
        unknown_cap = sum(
            1
            for r in er
            if r.strategy and r.strategy.capability and r.strategy.capability.verified is None
        )
        cap_ok = [
            r.strategy.capability.verified
            for r in er
            if r.strategy and r.strategy.capability and r.strategy.capability.verified is not None
        ]
        base_same = [
            base[(r.task_id, r.rep)].verified
            for r in er
            if (r.task_id, r.rep) in base and base[(r.task_id, r.rep)].status != "UNKNOWN"
        ]
        fb = [r for r in er if r.strategy and r.strategy.fallback.triggered]
        rec = [
            r
            for r in fb
            if r.strategy
            and r.strategy.capability
            and r.strategy.capability.verified is False
            and r.strategy.final_verified is not None
        ]
        rec_ok = [bool(r.strategy.final_verified) for r in rec if r.strategy]
        rec_base = [base[(r.task_id, r.rep)].verified for r in rec if (r.task_id, r.rep) in base]
        final = [r.verified for r in er if r.status != "UNKNOWN"]
        ct = sum(r.strategy.capability.tokens for r in er if r.strategy and r.strategy.capability)
        ft = sum(r.strategy.fallback.tokens for r in er if r.strategy)
        causes = Counter(
            r.strategy.fallback.cause.value for r in fb if r.strategy and r.strategy.fallback.cause
        )
        lines.append(
            f"| `{ref}` | {len(er)} (capability outcome unknown: {unknown_cap}) | {_pct(sum(cap_ok) / len(cap_ok) if cap_ok else None)} | {_pct(sum(base_same) / len(base_same) if base_same else None)} | {len(fb)}/{len(er)} ({_pct(len(fb) / len(er) if er else None)}) | {sum(rec_ok)}/{len(rec_ok)} ({_pct(sum(rec_ok) / len(rec_ok) if rec_ok else None)}) | {_pct(sum(rec_base) / len(rec_base) if rec_base else None)} | {_pct(sum(final) / len(final) if final else None)} | {ct:,} | {ft:,} | {(ft / (ct + ft)) if ct + ft else 0:.0%} | {dict(causes) or '-'} |"
        )
    return lines


def _verdict_label(x: Verdict) -> str:
    return x.value


def _decisions(evals: Sequence[AssetServingEvaluation]) -> list[str]:
    v = _verdict_label
    lines = [
        "| Set | Asset | in-scope tasks | Safety | Intrinsic (Layer A) | Serving improves (Layer B) | Economic | Classification |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in evals:
        lines.append(
            f"| {e.set_name} | `{e.asset_ref}` | {e.in_scope_tasks} | {v(e.safety.verdict)} | {v(e.intrinsic.verdict)} | {v(e.serving.verdict)} | {v(e.economic.verdict)} | {e.classification.value} |"
        )
    lines += ["", "Reasons and evidence requirements:", ""]
    for e in evals:
        lines.append(f"* `{e.set_name}:{e.asset_ref}`")
        lines.append(
            f"  * safety: {'; '.join(e.safety.reasons)} (untested: {'; '.join(e.safety.untested)})"
        )
        for name, d in (
            ("intrinsic", e.intrinsic),
            ("serving", e.serving),
            ("economic", e.economic),
        ):
            req = f" **Requires:** {d.requirement}." if d.requirement else ""
            lines.append(f"  * {name} {v(d.verdict)}: {'; '.join(d.reasons)}.{req}")
    return lines


def _economics(evals: Sequence[AssetServingEvaluation]) -> list[str]:
    lines = [
        "| Set | Asset | R | learning tokens (discovery+analysis) | evaluation tokens (strategy + baseline, unshared) | lifecycle tokens | break-even (verified successes) | tokens per verified improvement |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in evals:
        for r in e.economics:
            be = "never" if r.break_even_successes is None else f"{r.break_even_successes:,.0f}"
            ci = (
                "n/a"
                if r.cost_per_verified_improvement is None
                else f"{r.cost_per_verified_improvement:,.0f}"
            )
            note = f" ({r.note})" if r.note and r.revalidations == 0 else ""
            lines.append(
                f"| {e.set_name} | `{e.asset_ref}` | {r.revalidations} | {r.learning_tokens:,} | {r.evaluation_tokens:,} | {r.lifecycle_tokens:,} | {be}{note} | {ci} |"
            )
    return lines


def _applicability(rows: Sequence[RunRow]) -> list[str]:
    lines = [
        "| Arm | asset | applied in-family | applied out-of-family (false application) | in-family tasks with no asset applied |",
        "|---|---|---|---|---|",
    ]
    keys = sorted(
        {
            (r.arm, r.asset_ref, r.asset_family)
            for r in rows
            if r.arm in ("B", "C", "D", "E") and r.asset_ref
        }
    )
    for arm, ref, fam in keys:
        applied = [r for r in rows if r.arm == arm and r.asset_ref == ref]
        wrong = [r for r in applied if r.family != fam]
        fam_tasks = [r for r in rows if r.arm == arm and r.family == fam]
        missed = [r for r in fam_tasks if r.asset_ref != ref]
        lines.append(
            f"| {ARM_LABEL[arm]} | `{ref}` | {len(applied) - len(wrong)} | {len(wrong)} | {len(missed)}/{len(fam_tasks)} |"
        )
    return lines


def render_serving_report(
    results: Sequence[tuple[str, ExperimentResult]],
    *,
    title: str,
    policy: ServingPolicy | None = None,
) -> tuple[str, list[AssetServingEvaluation]]:
    pol = policy or serving_low_v1()
    all_evals: list[AssetServingEvaluation] = []
    lines = [
        f"# {title}",
        "",
        f"Generated from recorded runs by `ee-exp serving-report`. Serving policy `{pol.name}` (hash `{pol.policy_hash()[:12]}…`), frozen in [production-aligned-evaluation-design.md](../experiments/production-aligned-evaluation-design.md) §4 before the data existed. **The policy is applied as a counterfactual to experiment rows: no asset was promoted, activated or changed.** E1 is derived from the capability phase of the E2 production runs (same runs, same tasks), never executed separately.",
        "",
    ]
    for set_name, res in results:
        rows = list(res.rows)
        attributed = any(r.strategy is not None for r in rows)
        e1 = derive_e1_rows(rows)
        allr = rows + e1
        arms = [a for a in ("A", "B", "C", "D", "E1", "E", "F") if any(r.arm == a for r in allr)]
        cfg = res.config
        lines += [
            f"## Set {set_name}",
            "",
            f"Model `{cfg.model.get('model')}`; repetitions {cfg.repetitions}; asset states readable: {cfg.asset_states}; capabilities offered: {', '.join(cfg.capabilities) or 'none'}; memory assets: {', '.join(cfg.memory_assets) or 'none'}. Runs {len(rows)} ({sum(r.status == 'UNKNOWN' for r in rows)} UNKNOWN, missing data); distinct tasks {len({r.task_id for r in rows})}; phase attribution recorded: {'yes' if attributed else '**no** (E1 and intrinsic decisions unavailable)'}."
            + (
                " **Experimental asset states: candidate assets were readable; this is a research run, not promotion evidence.**"
                if cfg.asset_states != ["ACTIVE"]
                else ""
            ),
            "",
            "### Success by family (mean of per-task success; n = distinct tasks)",
            "",
            *_family_table(allr, arms),
            "",
            "### Success by variant",
            "",
            *_variant_table(allr, arms),
            "",
            "### Paired comparisons with the stateless baseline (task level; Holm across this table)",
            "",
            *_compare_table(allr, [a for a in arms if a != "A"]),
            "",
            "### Stratified production estimate (strata = family; weights = sampled task counts)",
            "",
            *_stratified(rows, [a for a in arms if a not in ("E1",)], pol),
            "",
            "### Cost, tool calls and latency (all runs; fallback cost is inside E2)",
            "",
            *_cost_table(
                allr, arms + [a for a in ("UB", "UC", "UD") if any(r.arm == a for r in allr)]
            ),
            "",
        ]
        if attributed:
            lines += ["### Capability / fallback attribution", "", *_attribution(rows), ""]
        lines += ["### Applicability", "", *_applicability(rows), ""]
        ung = [a for a in ("UB", "UC", "UD") if any(r.arm == a for r in rows)]
        if ung:
            fams = sorted({r.family for r in rows if r.arm in ung})
            lines += ["### Negative transfer (ungated memory on families never learned)", ""]
            for fam in fams:
                tasks = {r.task_id for r in rows if r.family == fam}
                lines.append(
                    f"Family `{fam}` ({len(tasks)} tasks): "
                    + "; ".join(
                        f"{ARM_LABEL[a]} {_pct(_mean(rows, a, tasks))}"
                        for a in ("A", *ung, "B", "C", "D", "E")
                        if any(r.arm == a and r.family == fam for r in rows)
                    )
                )
            lines.append("")
        if attributed:
            evals = evaluate_assets(
                allr, pol, set_name=set_name, ledger_by_family=res.ledger_by_family
            )
            all_evals += evals
    lines += [
        "## Asset decisions (three separate questions + mandatory safety)",
        "",
        *_decisions(all_evals),
        "",
        "## Lifecycle economics (explicit assumptions)",
        "",
        "Learning = discovery + analysis tokens of the asset's family; creation costs 0 model tokens (deterministic); evaluation = this experiment's strategy and baseline runs on the in-scope tasks, charged without sharing; R = assumed full re-evaluations over the asset's life (**not measured**). Break-even is in verified successes against the baseline's tokens per verified success; the strategy's tokens include any failed capability attempt and the fallback. Tokens per verified improvement = lifecycle tokens / extra verified successes over one pass of the in-scope tasks.",
        "",
        *_economics(all_evals),
    ]
    return "\n".join(lines) + "\n", all_evals


def _mean(rows: Sequence[RunRow], arm: str, tasks: set[str]) -> float | None:
    rt = _rate_by_task(rows, arm, tasks)
    return sum(rt.values()) / len(rt) if rt else None
