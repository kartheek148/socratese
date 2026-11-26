"""Serving-policy evaluation (pure, experimental): three separate decisions per learned asset.

Inputs are recorded experiment rows (with `strategy` attribution); nothing is executed, promoted or
persisted here. See docs/experiments/production-aligned-evaluation-design.md section 4.
"""
# ruff: noqa: E501

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.experiment import LearningLedger, RunRow
from experience_engine.domain.serving import (
    AssetServingEvaluation,
    Classification,
    Decision,
    EconomicsRow,
    PhaseStats,
    SafetyResult,
    ServingPolicy,
)
from experience_engine.evaluation import task_stats as ts

ASSET_ARMS = ("B", "C", "D", "E")


def outcome_of(r: RunRow) -> bool | None:
    """Final verified outcome of a run; UNKNOWN is missing data (None), never a failure."""
    return None if r.status == "UNKNOWN" else r.verified


def _rate(vals: Sequence[bool | None]) -> float | None:
    known = [v for v in vals if v is not None]
    return sum(known) / len(known) if known else None


@dataclass
class _Scope:
    tasks: list[str]
    base: dict[str, list[RunRow]]
    arm: dict[str, list[RunRow]]
    variant: dict[str, str]


def _scope(rows: Sequence[RunRow], arm: str, family: str) -> _Scope:
    base: dict[str, list[RunRow]] = defaultdict(list)
    armr: dict[str, list[RunRow]] = defaultdict(list)
    variant: dict[str, str] = {}
    for r in rows:
        if r.family != family:
            continue
        if r.arm == "A":
            base[r.task_id].append(r)
        elif r.arm == arm:
            armr[r.task_id].append(r)
        variant[r.task_id] = r.variant
    tasks = sorted(
        t
        for t in base.keys() & armr.keys()
        if _rate([outcome_of(x) for x in base[t]]) is not None
        and _rate([outcome_of(x) for x in armr[t]]) is not None
    )
    return _Scope(tasks, base, armr, variant)


def _binary(rows: Sequence[RunRow]) -> bool | None:
    return ts.task_binary([outcome_of(r) for r in rows])


def _regression(
    sc: _Scope, strategy_binary: Mapping[str, bool | None], only: bool = False
) -> tuple[int, int, int]:
    """(events, baseline-solved tasks with a determinate strategy outcome, indeterminate tasks).
    `only=True` restricts to tasks present in `strategy_binary` (e.g. attempted tasks)."""
    events = solved = indeterminate = 0
    for t in sc.tasks:
        if only and t not in strategy_binary:
            continue
        b = _binary(sc.base[t])
        s = strategy_binary.get(t)
        if b is None or (b and s is None):
            indeterminate += int(b is None or s is None)
            continue
        if b:
            solved += 1
            events += int(not s)
    return events, solved, indeterminate


def _regression_gate(
    policy: ServingPolicy, events: int, solved: int, label: str
) -> tuple[Verdict, str, str | None]:
    be = ts.binomial_evidence(events, solved, policy.max_regression_rate_ucb, policy.confidence)
    desc = (
        f"{label} regression {events}/{solved} baseline-solved tasks, exact "
        f"{policy.confidence:.0%} bounds [{be.lcb:.0%}, {be.ucb:.0%}]"
    )
    if solved > 0 and be.lcb > policy.max_regression_rate_ucb:
        return Verdict.FAIL, f"{desc}: above tolerance {policy.max_regression_rate_ucb:.0%}", None
    if solved < policy.min_baseline_solved_tasks or be.ucb > policy.max_regression_rate_ucb:
        need = be.additional_needed
        req = (
            f"{need} more independent baseline-solved tasks (at the observed {events} regressions; "
            f"{policy.min_baseline_solved_tasks} minimum)"
            if need is not None
            else "regression risk cannot be bounded within the tolerance at this event rate"
        )
        return (
            Verdict.INCONCLUSIVE,
            f"{desc}: cannot show <= {policy.max_regression_rate_ucb:.0%}",
            req,
        )
    return Verdict.PASS, desc, None


def _combine(fails: list[str], opens: list[tuple[str, str | None]], ok: list[str]) -> Decision:
    if fails:
        return Decision(verdict=Verdict.FAIL, reasons=fails + [o[0] for o in opens])
    if opens:
        reqs = [o[1] for o in opens if o[1]]
        return Decision(
            verdict=Verdict.INCONCLUSIVE,
            reasons=[o[0] for o in opens],
            requirement="; ".join(reqs) if reqs else None,
        )
    return Decision(verdict=Verdict.PASS, reasons=ok)


def _safety(
    policy: ServingPolicy, arm_rows: Sequence[RunRow], ref: str, family: str
) -> SafetyResult:
    crit = false_app = budget = checked = missing = 0
    for r in arm_rows:
        in_play = r.asset_ref == ref or r.family == family
        if not in_play:
            continue
        checked += 1
        s = r.strategy
        reasons = set()
        if r.termination:
            reasons.add(r.termination.split(":")[0])
        if s is not None:
            if s.capability is not None:
                reasons.add(s.capability.reason)
            if s.final_termination:
                reasons.add(s.final_termination)
        crit += int(bool(reasons & set(policy.critical_reasons)))
        if r.asset_ref == ref and r.family != family:
            false_app += 1
        if s is None:
            missing += 1
        elif (
            s.budget_tokens > 0
            and s.total_tokens > (1 + policy.budget_overshoot_tolerance) * s.budget_tokens
        ):
            budget += 1
    fails = []
    if crit > policy.max_critical_violations:
        fails.append(f"{crit} critical violation(s) (TOOL_NOT_ALLOWED / TENANT_MISMATCH)")
    if false_app > policy.max_false_applications:
        fails.append(f"{false_app} false application(s) outside the declared family")
    if budget:
        fails.append(
            f"{budget} run(s) exceeded the budget by more than {policy.budget_overshoot_tolerance:.0%}"
        )
    untested = [
        "cross-tenant access (single-tenant benchmark)",
        "forbidden-resource modification (no protected resources in the simulated environment)",
    ]
    if missing:
        untested.append(f"{missing} run(s) without phase attribution (budget bound unchecked)")
    return SafetyResult(
        verdict=Verdict.FAIL if fails else Verdict.PASS,
        critical_violations=crit,
        false_applications=false_app,
        budget_violations=budget,
        runs_checked=checked,
        untested=untested,
        reasons=fails or ["no violation observed on the exercised constraints"],
    )


def _intrinsic(
    policy: ServingPolicy, sc: _Scope, kind: str, ref: str
) -> tuple[Decision, dict[str, object]]:
    strategy_binary: dict[str, bool | None] = {}
    unmeasured = False
    for t in sc.tasks:
        if kind == "CAPABILITY":
            vals: list[bool | None] = []
            attempted = False
            for r in sc.arm[t]:
                s = r.strategy
                if s is None or not s.attempted or s.asset_ref != ref or s.capability is None:
                    continue
                attempted = True
                vals.append(s.capability.verified)
                unmeasured |= not s.capability.outcome_measured
            if attempted:
                strategy_binary[t] = ts.task_binary(vals)
        else:
            applied = [r for r in sc.arm[t] if r.asset_ref == ref]
            if applied:
                strategy_binary[t] = _binary(applied)
    determinate = {t: v for t, v in strategy_binary.items() if v is not None}
    n = len(determinate)
    ok = sum(determinate.values())
    rate = ok / n if n else None
    events, solved, _ = _regression(sc, strategy_binary, only=True)
    stats: dict[str, object] = {
        "attempted_tasks": len(strategy_binary),
        "determinate_tasks": n,
        "success_rate": rate,
        "regression_events": events,
        "baseline_solved_tasks": solved,
    }
    fails: list[str] = []
    opens: list[tuple[str, str | None]] = []
    ok_reasons: list[str] = []
    if unmeasured:
        opens.append(
            (
                "capability-phase outcome was not measured (attribution disabled)",
                "rerun with phase attribution",
            )
        )
    if n < policy.min_attempted_tasks:
        opens.append(
            (f"{n} determinate attempted tasks; policy requires {policy.min_attempted_tasks}",
             f"{policy.min_attempted_tasks - n} more independent attempted tasks"),
        )  # fmt: skip
    elif rate is not None and rate < policy.min_success_rate:
        fails.append(
            f"asset-alone success {rate:.0%} < {policy.min_success_rate:.0%} over {n} tasks"
        )
    elif rate is not None:
        lb = ts.wilson_lower(ok, n, policy.confidence)
        stats["success_lower_bound"] = lb
        if lb < policy.min_success_lower_bound:
            opens.append(
                (
                    f"success {rate:.0%} with lower bound {lb:.0%} < {policy.min_success_lower_bound:.0%}",
                    "more independent attempted tasks",
                )
            )
        else:
            ok_reasons.append(
                f"asset-alone success {rate:.0%} over {n} tasks (lower bound {lb:.0%})"
            )
    v, text, req = _regression_gate(policy, events, solved, "asset-alone")
    if v == Verdict.FAIL:
        fails.append(text)
    elif v == Verdict.INCONCLUSIVE and n >= policy.min_attempted_tasks:
        opens.append((text, req))
    elif v == Verdict.PASS:
        ok_reasons.append(text)
    return _combine(fails, opens, ok_reasons), stats


def _serving(
    policy: ServingPolicy, sc: _Scope
) -> tuple[Decision, dict[str, object], dict[str, bool | None]]:
    strategy_binary = {t: _binary(sc.arm[t]) for t in sc.tasks}
    diffs = [
        (_rate([outcome_of(x) for x in sc.arm[t]]) or 0.0)
        - (_rate([outcome_of(x) for x in sc.base[t]]) or 0.0)
        for t in sc.tasks
    ]
    mean, lo, hi, _ = ts.paired_difference(
        diffs, policy.confidence, resamples=policy.bootstrap_resamples, seed=policy.bootstrap_seed
    )
    events, solved, indeterminate = _regression(sc, strategy_binary)
    stats: dict[str, object] = {
        "in_scope_tasks": len(sc.tasks),
        "paired_diff": mean,
        "paired_diff_lcb": lo,
        "paired_diff_ucb": hi,
        "regression_events": events,
        "baseline_solved_tasks": solved,
        "indeterminate_tasks": indeterminate,
        "improved_tasks": sum(1 for d in diffs if d > 0),
        "worse_tasks": sum(1 for d in diffs if d < 0),
    }
    fails: list[str] = []
    opens: list[tuple[str, str | None]] = []
    ok: list[str] = []
    if len(sc.tasks) < policy.min_in_scope_tasks:
        opens.append((f"{len(sc.tasks)} in-scope tasks; policy requires {policy.min_in_scope_tasks}",
                      f"{policy.min_in_scope_tasks - len(sc.tasks)} more independent in-scope tasks"))  # fmt: skip
        return _combine(fails, opens, ok), stats, strategy_binary
    desc = (
        f"paired final-success difference {mean:+.0%} over {len(sc.tasks)} tasks, "
        f"{policy.confidence:.0%} one-sided bounds [{lo:+.0%}, {hi:+.0%}]"
        if lo is not None and hi is not None and mean is not None
        else "paired difference bounds unavailable"
    )
    if hi is not None and hi < -policy.harm_margin:
        fails.append(f"{desc}: worse than the baseline by more than {policy.harm_margin:.0%}")
    elif lo is not None and lo > 0:
        ok.append(desc)
    else:
        opens.append(
            (f"{desc}: an improvement is not demonstrated", "more independent in-scope tasks")
        )
    v, text, req = _regression_gate(policy, events, solved, "serving")
    if v == Verdict.FAIL:
        fails.append(text)
    elif v == Verdict.INCONCLUSIVE:
        opens.append((text, req))
    else:
        ok.append(text)
    return _combine(fails, opens, ok), stats, strategy_binary


def _cost_units(sc: _Scope) -> list[ts.CostUnit]:
    units = []
    for t in sc.tasks:
        s_known = [r for r in sc.arm[t] if r.status != "UNKNOWN"]
        b_known = [r for r in sc.base[t] if r.status != "UNKNOWN"]
        units.append(
            ts.CostUnit(
                sum(r.tokens for r in s_known) / len(s_known),
                sum(r.verified for r in s_known) / len(s_known),
                sum(r.tokens for r in b_known) / len(b_known),
                sum(r.verified for r in b_known) / len(b_known),
            )
        )
    return units


def _economic(
    policy: ServingPolicy, units: Sequence[ts.CostUnit], serving: Decision, safety: SafetyResult
) -> tuple[Decision, dict[str, object]]:
    point, lo, hi, undefined = ts.cost_per_success_ratio(
        units, policy.confidence, resamples=policy.bootstrap_resamples, seed=policy.bootstrap_seed
    )
    stats: dict[str, object] = {
        "cost_ratio": point,
        "cost_ratio_lcb": lo,
        "cost_ratio_ucb": hi,
        "undefined_resamples": undefined,
    }  # noqa: E501
    if point is None or lo is None or hi is None:
        return Decision(
            verdict=Verdict.INCONCLUSIVE,
            reasons=[
                "tokens per verified success undefined or unstable (no verified successes in a resample)"
            ],
            requirement="more independent tasks",
        ), stats  # noqa: E501
    desc = (
        f"total tokens per verified success (incl. any failed capability attempt and the fallback) "
        f"{point:.2f}x the baseline's, {policy.confidence:.0%} one-sided bounds [{lo:.2f}, {hi:.2f}]"
    )
    if lo > policy.max_cost_ratio:
        return Decision(
            verdict=Verdict.FAIL, reasons=[f"{desc}: more expensive than the baseline"]
        ), stats
    if hi < policy.max_cost_ratio:
        if serving.verdict != Verdict.PASS or safety.verdict != Verdict.PASS:
            return Decision(
                verdict=Verdict.INCONCLUSIVE,
                reasons=[f"{desc}; savings never count unless safety and serving pass"],
            ), stats  # noqa: E501
        return Decision(verdict=Verdict.PASS, reasons=[desc]), stats
    return Decision(
        verdict=Verdict.INCONCLUSIVE,
        reasons=[f"{desc}: cheaper per success is not demonstrated"],
        requirement="more independent tasks",
    ), stats  # noqa: E501


def classify(
    intrinsic: Decision, serving: Decision, economic: Decision, safety: SafetyResult,
    recovered: bool,
) -> Classification:  # fmt: skip
    if safety.verdict == Verdict.FAIL or serving.verdict == Verdict.FAIL:
        return Classification.HARMFUL
    if Verdict.INCONCLUSIVE in (intrinsic.verdict, serving.verdict, economic.verdict):
        return Classification.INSUFFICIENT
    if intrinsic.verdict == Verdict.PASS and serving.verdict == Verdict.PASS:
        if economic.verdict == Verdict.PASS:
            return Classification.RELIABLE_AND_USEFUL
        return Classification.RELIABLE_NOT_WORTH_COST
    if intrinsic.verdict == Verdict.FAIL and serving.verdict == Verdict.PASS and recovered:
        if economic.verdict == Verdict.FAIL:
            return Classification.USEFUL_BUT_COSTLY
        return Classification.IMPERFECT_BUT_USEFUL_WITH_FALLBACK
    return Classification.INSUFFICIENT


def _phases(sc: _Scope, kind: str, ref: str) -> PhaseStats:
    cap_vals: list[bool | None] = []
    attempted = fb_trig = fb_att = 0
    cap_tok = fb_tok = cap_lat = fb_lat = calls = unknown = 0
    rec_final: list[bool] = []
    rec_base: list[bool] = []
    final: list[bool | None] = []
    indeterminate = 0
    for t in sc.tasks:
        final.append(_binary(sc.arm[t]))
        for r in sc.arm[t]:
            unknown += int(r.status == "UNKNOWN")
            calls += r.tool_calls
            s = r.strategy
            if kind != "CAPABILITY" or s is None or not s.attempted or s.asset_ref != ref:
                continue
            attempted += 1
            if s.capability is not None:
                cap_vals.append(s.capability.verified)
                cap_tok += s.capability.tokens
                cap_lat += s.capability.latency_ms
            if s.fallback.triggered:
                fb_trig += 1
                fb_att += int(s.fallback.attempted)
                fb_tok += s.fallback.tokens
                fb_lat += s.fallback.latency_ms
                if (
                    s.capability is not None
                    and s.capability.verified is False
                    and s.final_verified is not None
                ):
                    rec_final.append(bool(s.final_verified))
                    b = _rate([outcome_of(x) for x in sc.base[t]])
                    rec_base.append(bool(b is not None and b >= 0.5))
    known_cap = [v for v in cap_vals if v is not None]
    fk = [v for v in final if v is not None]
    indeterminate = sum(1 for v in final if v is None)
    return PhaseStats(
        attempted_tasks=attempted,
        capability_success_rate=(sum(known_cap) / len(known_cap)) if known_cap else None,
        fallback_triggered_tasks=fb_trig,
        fallback_frequency=(fb_trig / attempted) if attempted else None,
        fallback_attempted_tasks=fb_att,
        recovery_rate=(sum(rec_final) / len(rec_final)) if rec_final else None,
        baseline_rate_on_recovery_tasks=(sum(rec_base) / len(rec_base)) if rec_base else None,
        final_success_rate=(sum(fk) / len(fk)) if fk else None,
        capability_tokens=cap_tok,
        fallback_tokens=fb_tok,
        capability_latency_ms=cap_lat,
        fallback_latency_ms=fb_lat,
        total_tool_calls=calls,
        unknown_runs=unknown,
        indeterminate_tasks=indeterminate,
    )


def _economics(
    policy: ServingPolicy, sc: _Scope, family: str, ledgers: Mapping[str, LearningLedger]
) -> list[EconomicsRow]:
    led = ledgers.get(family)
    learning = (led.discovery_tokens + led.analysis_tokens) if led else 0
    s_rows = [r for t in sc.tasks for r in sc.arm[t]]
    b_rows = [r for t in sc.tasks for r in sc.base[t]]
    eval_tokens = sum(r.tokens for r in s_rows) + sum(r.tokens for r in b_rows)
    s_ver = sum(r.verified for r in s_rows if r.status != "UNKNOWN")
    b_ver = sum(r.verified for r in b_rows if r.status != "UNKNOWN")
    s_tok = sum(r.tokens for r in s_rows if r.status != "UNKNOWN")
    b_tok = sum(r.tokens for r in b_rows if r.status != "UNKNOWN")
    c_s = s_tok / s_ver if s_ver else None
    c_b = b_tok / b_ver if b_ver else None
    # normalise the improvement to runs per arm so unequal repetition counts cannot inflate it
    n_s = max(1, sum(1 for r in s_rows if r.status != "UNKNOWN"))
    n_b = max(1, sum(1 for r in b_rows if r.status != "UNKNOWN"))
    extra = s_ver / n_s - b_ver / n_b
    runs_gain = extra * len(sc.tasks)  # extra verified successes per pass over the in-scope tasks
    out = []
    for rv in policy.revalidation_assumptions:
        total = learning + eval_tokens * (1 + rv)
        if c_s is None or c_b is None:
            be, note = None, "no verified successes in an arm"
        elif c_s < c_b:
            be, note = total / (c_b - c_s), ""
        else:
            be, note = (
                None,
                "not cheaper per verified success: never recovered by execution savings",
            )
        out.append(
            EconomicsRow(
                revalidations=rv,
                learning_tokens=learning,
                evaluation_tokens=eval_tokens,
                lifecycle_tokens=total,
                break_even_successes=be,
                cost_per_verified_improvement=(total / runs_gain) if runs_gain > 0 else None,
                note=note,
            )
        )
    return out


def evaluate_assets(
    rows: Sequence[RunRow],
    policy: ServingPolicy,
    *,
    set_name: str,
    ledger_by_family: Mapping[str, LearningLedger] | None = None,
) -> list[AssetServingEvaluation]:
    ledgers = ledger_by_family or {}
    out: list[AssetServingEvaluation] = []
    seen = sorted({(r.arm, r.asset_ref, r.asset_kind, r.asset_family) for r in rows
                   if r.arm in ASSET_ARMS and r.asset_ref and r.asset_family})  # fmt: skip
    for arm, ref, kind, family in seen:
        assert ref and kind and family
        sc = _scope(rows, arm, family)
        arm_rows = [r for r in rows if r.arm == arm]
        safety = _safety(policy, arm_rows, ref, family)
        intrinsic, istats = _intrinsic(policy, sc, kind, ref)
        serving, sstats, _ = _serving(policy, sc)
        economic, estats = _economic(policy, _cost_units(sc), serving, safety)
        phases = _phases(sc, kind, ref).model_copy(
            update={
                "unknown_runs": sum(
                    1 for r in arm_rows if r.family == family and r.status == "UNKNOWN"
                )
            }
        )
        recovered = bool(
            phases.recovery_rate is not None
            and phases.baseline_rate_on_recovery_tasks is not None
            and phases.recovery_rate > phases.baseline_rate_on_recovery_tasks
        )
        variants: dict[str, dict[str, float]] = {}
        for v in sorted(set(sc.variant[t] for t in sc.tasks)):
            ts_v = [t for t in sc.tasks if sc.variant[t] == v]
            sr = [_rate([outcome_of(x) for x in sc.arm[t]]) or 0.0 for t in ts_v]
            br = [_rate([outcome_of(x) for x in sc.base[t]]) or 0.0 for t in ts_v]
            variants[v] = {
                "tasks": float(len(ts_v)),
                "strategy": sum(sr) / len(sr),
                "baseline": sum(br) / len(br),
            }
        out.append(
            AssetServingEvaluation(
                set_name=set_name,
                asset_ref=ref,
                asset_kind=kind,
                arm=arm,
                family=family,
                policy_name=policy.name,
                policy_hash=policy.policy_hash(),
                in_scope_tasks=len(sc.tasks),
                safety=safety,
                intrinsic=intrinsic,
                serving=serving,
                economic=economic,
                classification=classify(intrinsic, serving, economic, safety, recovered),
                stats={**istats, **{f"serving_{k}": v for k, v in sstats.items()}, **estats},
                phases=phases,
                economics=_economics(policy, sc, family, ledgers),
                by_variant=variants,
            )
        )
    return out
