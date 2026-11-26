"""Fresh confirmation of the candidate promotion policy (pure analysis).

Inputs are recorded evaluations (each carrying the governing verdict and the candidate policy's
counterfactual on the SAME evidence) and an independent forced-ACTIVE comparison on a different
private seed that supplies the truth labels. The protocol, labels and decision rule are frozen in
docs/experiments/promotion-policy-v2-design.md section 9 before any data existed. Nothing here
promotes an asset or changes a policy.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from experience_engine.domain.evaluation import (
    EvaluationReport,
    RiskBasedPolicy,
    Suite,
    TaskResult,
    Verdict,
    default_policies_v2,
)
from experience_engine.domain.experiment import RunRow
from experience_engine.evaluation import paired
from experience_engine.evaluation.policy_simulation import p2_task_evidence
from experience_engine.evaluation.risk import paired_bootstrap_bounds
from experience_engine.evaluation.sequential import RunPair, StagePlan, stop_reasons
from experience_engine.evaluation.validation_cost import (
    total_cached_tokens,
    total_executed_tokens,
)

MEM_ARM = {"episodic": "B", "consolidated": "C", "procedural": "D"}
PORTFOLIO_ORDER = ("E", "D", "C", "B")  # capability first, then memory by decreasing structure
BENEFICIAL_MAX_REGRESSION = 0.25
HARMFUL_MIN_REGRESSION = 0.5


def asset_family(asset_id: str, kind: str) -> str:
    return asset_id.split("_", 2)[2] if kind == "MEMORY" else asset_id.split(".")[0]


def asset_arm(asset_id: str, kind: str) -> str:
    return MEM_ARM[asset_id.split("_")[1]] if kind == "MEMORY" else "E"


@dataclass(frozen=True)
class AssetDecision:
    set_name: str
    asset: str  # id@version
    kind: str
    family: str
    arm: str
    governing_policy: str
    governing: Verdict
    governing_reasons: list[str]
    candidate_policy: str
    candidate: Verdict
    candidate_reasons: list[str]


def decisions_from_reports(
    reports: Sequence[EvaluationReport], set_name: str
) -> list[AssetDecision]:
    out = []
    for r in reports:
        cf = next((c for c in r.counterfactuals if c.policy_name.endswith("-v2")), None)
        if cf is None:
            raise ValueError(f"{r.capability_id}: no candidate-policy counterfactual recorded")
        out.append(
            AssetDecision(
                set_name=set_name,
                asset=f"{r.capability_id}@{r.version}",
                kind=r.asset_kind,
                family=asset_family(r.capability_id, r.asset_kind),
                arm=asset_arm(r.capability_id, r.asset_kind),
                governing_policy=r.policy_name,
                governing=r.verdict,
                governing_reasons=r.reasons,
                candidate_policy=cf.policy_name,
                candidate=cf.verdict,
                candidate_reasons=cf.reasons,
            )
        )
    return sorted(out, key=lambda d: (d.family, d.kind, d.asset))


@dataclass(frozen=True)
class Truth:
    label: str  # beneficial | harmful | unclear
    tasks: int
    diff: float
    diff_lcb: float
    diff_ucb: float
    regression_rate: float | None
    baseline_solved: int
    regressed: int


def truth_label(
    rows: Sequence[RunRow],
    asset: str,
    arm: str,
    policy: RiskBasedPolicy | None = None,
) -> Truth:
    pol = policy or default_policies_v2()["low"]
    ev = p2_task_evidence([r.model_dump() for r in rows], asset, arm)
    mean, lo, hi = paired_bootstrap_bounds(
        ev.paired_diffs, pol.confidence, resamples=pol.bootstrap_resamples, seed=pol.bootstrap_seed
    )
    reg = ev.regression_mass / ev.regression_tasks if ev.regression_tasks else None
    if (hi < 0 and ev.paired_diffs) or (reg is not None and reg > HARMFUL_MIN_REGRESSION):
        label = "harmful"
    elif ev.paired_diffs and lo > 0 and (reg is None or reg <= BENEFICIAL_MAX_REGRESSION):
        label = "beneficial"
    else:
        label = "unclear"
    return Truth(
        label, len(ev.paired_diffs), mean, lo, hi, reg, ev.regression_tasks, ev.n_regressions
    )


def standalone_capability_rows(rows: Sequence[RunRow]) -> list[RunRow]:
    """Gate-aligned view of the capability arm: the gate evaluates the capability ALONE, while the
    production runtime (arm E) falls back to general planning when it fails. A run that fell back is
    a failure of the capability itself. Memory arms are unchanged."""
    out = []
    for r in rows:
        if r.arm == "E" and r.fell_back and r.status != "UNKNOWN":
            r = r.model_copy(update={"verified": False, "status": "FAILURE"})
        out.append(r)
    return out


def classify(d: AssetDecision, truth: Truth) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    for name, v in (("governing", d.governing), ("candidate", d.candidate)):
        out[name] = {
            "unjustified_rejection": truth.label == "beneficial" and v == Verdict.FAIL,
            "unjustified_non_promotion": truth.label == "beneficial" and v != Verdict.PASS,
            "harmful_promotion": truth.label == "harmful" and v == Verdict.PASS,
            "beneficial_promoted": truth.label == "beneficial" and v == Verdict.PASS,
        }
    return out


def portfolio_rows(
    rows: Sequence[RunRow], promoted: dict[str, list[tuple[str, str]]], label: str
) -> list[RunRow]:
    """What production would serve per P4 task: the first promoted asset of the family in
    PORTFOLIO_ORDER (its arm's recorded outcome), else the baseline's own outcome."""
    by_task: dict[tuple[str, int], dict[str, RunRow]] = defaultdict(dict)
    for r in rows:
        by_task[(r.task_id, r.rep)][r.arm] = r
    out = []
    for (_task, _rep), arms in sorted(by_task.items()):
        base = arms.get("A")
        if base is None:
            continue
        chosen = base
        for arm in PORTFOLIO_ORDER:
            want = dict(promoted.get(base.family, [])).get(arm)
            got = arms.get(arm)
            if want is not None and got is not None and got.asset_ref == want:
                chosen = got
                break
        out.append(chosen.model_copy(update={"arm": label}))
    return out


@dataclass(frozen=True)
class PortfolioComparison:
    scope: str
    n_tasks: int
    success_baseline: float
    success_v1: float
    success_v2: float
    diff: float
    ci_lo: float
    ci_hi: float
    only_v2: int
    only_v1: int
    p_mcnemar: float
    p_holm: float = 1.0
    reg_v1: float | None = None  # P(fail | baseline succeeded)
    reg_v2: float | None = None
    reg_diff: float | None = None
    reg_ci_lo: float | None = None
    reg_ci_hi: float | None = None
    reg_tasks: int = 0
    reg_only_v2_fail: int = 0
    reg_only_v1_fail: int = 0
    reg_p: float = 1.0


def compare_portfolios(
    rows: Sequence[RunRow],
    promoted_v1: dict[str, list[tuple[str, str]]],
    promoted_v2: dict[str, list[tuple[str, str]]],
) -> list[PortfolioComparison]:
    p1 = portfolio_rows(rows, promoted_v1, "V1")
    p2 = portfolio_rows(rows, promoted_v2, "V2")
    base = [r for r in rows if r.arm == "A"]
    allrows = [*base, *p1, *p2]
    scopes: list[tuple[str, set[str] | None]] = [("all families", None)]
    for fam in sorted({r.family for r in base}):
        scopes.append((fam, {r.task_id for r in base if r.family == fam}))
    out: list[PortfolioComparison] = []
    for name, tasks in scopes:

        def in_scope(r: RunRow, t: set[str] | None = tasks) -> bool:
            return t is None or r.task_id in t

        c = paired.compare(allrows, "V2", "V1", where=in_scope)
        a = paired.compare(allrows, "V1", "A", where=in_scope)
        b = paired.compare(allrows, "V2", "A", where=in_scope)
        solved = {
            t for t, rate in paired.task_rates([r for r in base if in_scope(r)], "A").items()
            if rate >= 0.5
        }  # fmt: skip

        def in_solved(r: RunRow, s: set[str] = solved) -> bool:
            return r.task_id in s

        rc = paired.compare(allrows, "V2", "V1", where=in_solved)
        out.append(
            PortfolioComparison(
                scope=name,
                n_tasks=c.n_tasks,
                success_baseline=a.rate_y,
                success_v1=a.rate_x,
                success_v2=b.rate_x,
                diff=c.diff,
                ci_lo=c.ci_lo,
                ci_hi=c.ci_hi,
                only_v2=c.only_x,
                only_v1=c.only_y,
                p_mcnemar=c.p_mcnemar,
                reg_v1=1 - rc.rate_y if rc.n_tasks else None,
                reg_v2=1 - rc.rate_x if rc.n_tasks else None,
                reg_diff=-rc.diff if rc.n_tasks else None,  # regression-rate change (v2 - v1)
                reg_ci_lo=-rc.ci_hi if rc.n_tasks else None,
                reg_ci_hi=-rc.ci_lo if rc.n_tasks else None,
                reg_tasks=rc.n_tasks,
                reg_only_v2_fail=rc.only_y,  # solved by v1 but not v2
                reg_only_v1_fail=rc.only_x,
                reg_p=rc.p_mcnemar,
            )
        )
    adj = paired.holm({f"{o.scope}/success": o.p_mcnemar for o in out}
                      | {f"{o.scope}/regression": o.reg_p for o in out})  # fmt: skip
    return [PortfolioComparison(**{**o.__dict__, "p_holm": adj[f"{o.scope}/success"]}) for o in out]


def promoted_by_policy(
    decisions: Sequence[AssetDecision], policy: str
) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for d in decisions:
        v = d.governing if policy == "governing" else d.candidate
        if v == Verdict.PASS:
            out[d.family].append((d.arm, d.asset))
    return dict(out)


def mcnemar_assets(
    decisions: Sequence[AssetDecision], truths: dict[str, Truth]
) -> dict[str, float]:
    """Exact McNemar on PASS vs not-PASS among beneficial assets (illustrative: tiny n)."""
    only_c = only_g = 0
    for d in decisions:
        if truths[f"{d.set_name}:{d.asset}"].label != "beneficial":
            continue
        g, c = d.governing == Verdict.PASS, d.candidate == Verdict.PASS
        only_c += int(c and not g)
        only_g += int(g and not c)
    return {"only_candidate": only_c, "only_governing": only_g,
            "p": paired.mcnemar_exact(only_c, only_g)}  # fmt: skip


@dataclass(frozen=True)
class StageReplay:
    asset: str
    stopped_stage: int | None
    reasons: list[str]
    tokens_not_spent: int  # simulated: executed tokens of runs a real staged run would skip
    tokens_executed: int


def _executed(rows: Sequence[TaskResult]) -> int:
    return sum(r.tokens for r in rows if not r.cache_hit)


def replay_stages(report: EvaluationReport, plan: StagePlan | None = None) -> StageReplay:
    plan = plan or StagePlan()
    rows = report.task_results
    asset = f"{report.capability_id}@{report.version}"
    total = _executed(rows)
    if report.false_applicability_rate:
        later = [r for r in rows if r.suite != Suite.NEGATIVE]
        return StageReplay(asset, 1, ["stage 1: false applicability"], _executed(later), total)
    treat = report.treatment_arm
    held_t = [r for r in rows if r.suite == Suite.HELD_OUT and r.arm.value == treat]
    order: list[str] = []
    for r in held_t:
        if r.task_id not in order:
            order.append(r.task_id)
    seeds = sorted({r.seed for r in held_t})
    base = {
        (r.task_id, r.seed): r
        for r in rows
        if r.suite == Suite.HELD_OUT and r.arm.value == "A_BASELINE"
    }
    treat_by = {(r.task_id, r.seed): r for r in held_t}

    def pairs(tasks: set[str], seed_list: Sequence[int]) -> list[RunPair]:
        out = []
        for (tid, sd), t in treat_by.items():
            b = base.get((tid, sd))
            if tid not in tasks or sd not in seed_list or b is None:
                continue
            if "UNKNOWN" in (t.status, b.status):
                continue
            out.append(RunPair(tid, b.status == "VERIFIED_SUCCESS", t.status == "VERIFIED_SUCCESS"))
        return out

    need = report.policy.min_heldout_success_rate
    screen = set(order[: plan.screening_tasks])
    why = stop_reasons(
        pairs(screen, seeds[: plan.screening_seeds]),
        alpha=plan.alpha_screen,
        required_success_rate=need,
        stage=2,
    )
    if why:
        skipped = [
            r for r in rows
            if r.suite in (Suite.TRANSFER, Suite.SOURCE_REPLAY)
            or (r.suite == Suite.HELD_OUT and not (r.task_id in screen and r.seed == seeds[0]))
        ]  # fmt: skip
        return StageReplay(asset, 2, why, _executed(skipped), total)
    why = stop_reasons(
        pairs(set(order), seeds), alpha=plan.alpha_heldout, required_success_rate=need, stage=3
    )
    if why:
        skipped = [r for r in rows if r.suite in (Suite.TRANSFER, Suite.SOURCE_REPLAY)]
        return StageReplay(asset, 3, why, _executed(skipped), total)
    return StageReplay(asset, None, [], 0, total)


@dataclass(frozen=True)
class AssetCost:
    asset: str
    executed: int
    cached: int
    baseline_executed: int
    baseline_cached: int
    treatment_executed: int
    other_executed: int
    runs_executed: int
    runs_cached: int


def asset_cost(report: EvaluationReport) -> AssetCost:
    res = report.resources
    if res is None:
        raise ValueError(f"{report.capability_id}: report has no measured resources")
    base = res.by_arm.get("A_BASELINE")
    treat = res.by_arm.get(report.treatment_arm)
    be = base.tokens_executed if base else 0
    te = treat.tokens_executed if treat else 0
    ex = total_executed_tokens(res)
    return AssetCost(
        asset=f"{report.capability_id}@{report.version}",
        executed=ex,
        cached=total_cached_tokens(res),
        baseline_executed=be,
        baseline_cached=base.tokens_cached if base else 0,
        treatment_executed=te,
        other_executed=ex - be - te,
        runs_executed=sum(a.runs_executed for a in res.by_arm.values()),
        runs_cached=sum(a.runs_cached for a in res.by_arm.values()),
    )
