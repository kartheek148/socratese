"""Prequential full-information replay of a controller against the rule baseline.

Input: per-task outcomes of *every* feasible action (recorded paired experiments, or a labelled
synthetic world). The controller sees tasks one at a time in a seeded order, learns only from the
outcome of the action it chose (exploration cost is therefore paid and measured), and its
estimator is republished every `refresh_every` tasks (the learning plane's cadence). The rule
baseline always plays the default action. One recorded draw per task and action: this replays a
stochastic process, it is not a repeated measurement.
"""

import hashlib
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from experience_engine.controller.drift import MonitorFleet, references_from_snapshot
from experience_engine.controller.estimator import build_snapshot
from experience_engine.controller.features import features_for
from experience_engine.controller.paired import PairedPrior
from experience_engine.controller.perturb import Perturbation
from experience_engine.controller.policy import MODE_EXPLORE, MODE_PROBE, choose
from experience_engine.domain.controller import (
    CandidateFeature,
    ControllerKind,
    ControllerParams,
    ControllerPolicyDefinition,
    DecisionOutcomeRecord,
    Risk,
)
from experience_engine.domain.drift import DriftTransition
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.evaluation.task_stats import (
    CostUnit,
    cost_per_success_ratio,
    paired_difference,
)

NONINFERIORITY = 0.05
COST_MARGIN = 0.05
CELL_MARGIN = 0.10  # the controller's contractual per-cell non-inferiority margin (delta_low)


@dataclass(frozen=True)
class TaskOutcome:
    """What each feasible action would have produced for one task (full information)."""

    task_id: str
    family: str | None
    risk: Risk
    actions: list[CandidateFeature]
    default_key: str
    outcomes: dict[str, tuple[OutcomeStatus, int]]  # action key -> (status, tokens)


@dataclass(frozen=True)
class SimDrift:
    """A synthetic, labelled regime change: from stream position `start` on, each success of
    `action` that the controller observes survives with probability `survival`. The rule
    baseline's recorded outcomes are not touched."""

    start: int
    action: str
    survival: float


SIM_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)  # position i is observed at SIM_EPOCH + i hours
SIM_VERIFIER = "v-1"


def _unit(label: str, index: int) -> float:
    digest = hashlib.sha256(f"drift|{label}|{index}".encode()).hexdigest()
    return int(digest[:8], 16) / 0x1_0000_0000


@dataclass(frozen=True)
class SimConfig:
    kind: ControllerKind
    params: ControllerParams
    tenant_id: str = "bench"
    refresh_every: int = 10
    policy_id: str = "adaptive"
    # train each published snapshot on the last N logged outcomes only (recency under drift)
    train_window: int | None = None
    # M6.1: paired validation prior carried by every published snapshot (never a live record)
    prior: PairedPrior | None = None
    drift: SimDrift | None = None
    # M6.2: synthetic perturbation of the alternative's observed outcomes (labelled experiments)
    perturb: Perturbation | None = None


@dataclass(frozen=True)
class SimStep:
    index: int
    task_id: str
    family: str | None
    risk: Risk
    chosen: str
    default: str
    mode: str
    probability: float
    status: OutcomeStatus
    tokens: int
    default_status: OutcomeStatus
    default_tokens: int
    drift_state: str = "NORMAL"  # state of the alternative's cell the decision read

    @property
    def success(self) -> bool:
        return self.status == OutcomeStatus.VERIFIED_SUCCESS

    @property
    def default_success(self) -> bool:
        return self.default_status == OutcomeStatus.VERIFIED_SUCCESS

    @property
    def deviated(self) -> bool:
        return self.chosen != self.default

    @property
    def explored(self) -> bool:
        return self.mode == MODE_EXPLORE

    @property
    def probed(self) -> bool:
        return self.mode == MODE_PROBE


def make_orders(
    n_tasks: int, n_orders: int, seed: int, horizon: int | None = None
) -> list[list[int]]:
    """Seeded task orders. Default: each order is a permutation (every task once). With
    `horizon`, each order is a stream of that length drawn with replacement from the recorded
    tasks (a model-based extrapolation: a repeated draw re-uses the recorded outcome)."""
    orders = []
    for i in range(n_orders):
        rng = random.Random(seed * 1_000_003 + i)
        if horizon:
            orders.append([rng.randrange(n_tasks) for _ in range(horizon)])
        else:
            order = list(range(n_tasks))
            rng.shuffle(order)
            orders.append(order)
    return orders


def rule_log(tasks: Sequence[TaskOutcome], tenant_id: str = "bench") -> list[DecisionOutcomeRecord]:
    """What a rule-only production log looks like: the default action, propensity 1."""
    out = []
    for i, t in enumerate(tasks):
        status, tokens = t.outcomes[t.default_key]
        out.append(
            DecisionOutcomeRecord(
                decision_id=f"log-{i:06d}",
                run_id=f"log-{i:06d}",
                tenant_id=tenant_id,
                policy_version="rule-capability/1",
                selection_probability=1.0,
                family=t.family,
                risk_level=t.risk,
                action=t.default_key,
                status=status,
                tokens=tokens,
                actions=t.actions,
                default_key=t.default_key,
                propensities={t.default_key: 1.0},
            )
        )
    return out


@dataclass
class SimRun:
    steps: list[SimStep]
    transitions: list[DriftTransition] = field(default_factory=list)
    final_states: dict[str, str] = field(default_factory=dict)


def run_stream(
    tasks: Sequence[TaskOutcome],
    order: Sequence[int],
    cfg: SimConfig,
    warm: Sequence[DecisionOutcomeRecord] = (),
    label: str = "sim",
) -> SimRun:
    records = list(warm)
    definition: ControllerPolicyDefinition | None = None
    snap_hash = ""
    steps: list[SimStep] = []
    dp = cfg.params.drift
    fleet: MonitorFleet | None = None
    ingested = len(records)
    overlay = None
    for i, idx in enumerate(order):
        if definition is None or i % cfg.refresh_every == 0:
            train = records if cfg.train_window is None else records[-cfg.train_window :]
            snap = build_snapshot(train, cfg.tenant_id, cfg.params, prior=cfg.prior)
            definition = ControllerPolicyDefinition(
                policy_id=cfg.policy_id,
                version=1 + i // cfg.refresh_every,
                kind=cfg.kind,
                params=cfg.params,
                snapshot=snap,
            )
            snap_hash = snap.snapshot_hash()
            if dp is not None:
                if fleet is None:
                    fleet = MonitorFleet(references_from_snapshot(snap), dp)
                fleet.ingest(records[ingested:])
                ingested = len(records)
                overlay = fleet.overlay()
        t = tasks[idx]
        f = features_for(
            definition,
            snap_hash,
            decision_id=f"{label}-{i}",
            tenant_id=cfg.tenant_id,
            family=t.family,
            risk=t.risk,
            n_allowed_tools=0,
            actions=t.actions,
            default_key=t.default_key,
            overlay=overlay,
        )
        choice = choose(f, cfg.params)
        status, tokens = t.outcomes[choice.selected_key]
        d_status, d_tokens = t.outcomes[t.default_key]
        drift = cfg.drift
        if (
            drift is not None
            and i >= drift.start
            and choice.selected_key == drift.action
            and status == OutcomeStatus.VERIFIED_SUCCESS
            and _unit(label, i) >= drift.survival
        ):
            status = OutcomeStatus.FAILURE
        runtime = "rt-1"
        pt = cfg.perturb
        if pt is not None and choice.selected_key == pt.action:
            ok, tokens, runtime = pt.apply(
                i, t.family, status == OutcomeStatus.VERIFIED_SUCCESS, tokens
            )
            status = OutcomeStatus.VERIFIED_SUCCESS if ok else OutcomeStatus.FAILURE
        records.append(
            DecisionOutcomeRecord(
                decision_id=f"{label}-{i}",
                run_id=f"{label}-{i}",
                tenant_id=cfg.tenant_id,
                policy_version=definition.policy_version,
                selection_probability=choice.probability,
                family=t.family,
                risk_level=t.risk,
                action=choice.selected_key,
                status=status,
                tokens=tokens,
                actions=t.actions,
                default_key=t.default_key,
                propensities=choice.propensities,
                observed_at=SIM_EPOCH + timedelta(hours=i),
                runtime_version=runtime,
                verifier_version=SIM_VERIFIER,
            )
        )
        steps.append(
            SimStep(
                i,
                t.task_id,
                t.family,
                t.risk,
                choice.selected_key,
                t.default_key,
                choice.mode,
                choice.probability,
                status,
                tokens,
                d_status,
                d_tokens,
                f.drift[0].state.value if f.drift else "NORMAL",
            )
        )
    if fleet is not None:
        fleet.ingest(records[ingested:])
    return SimRun(
        steps,
        list(fleet.transitions) if fleet is not None else [],
        {k: s.state.value for k, s in fleet.states.items()} if fleet is not None else {},
    )


def run_sequence(
    tasks: Sequence[TaskOutcome],
    order: Sequence[int],
    cfg: SimConfig,
    warm: Sequence[DecisionOutcomeRecord] = (),
    label: str = "sim",
) -> list[SimStep]:
    return run_stream(tasks, order, cfg, warm, label).steps


@dataclass(frozen=True)
class ArmSummary:
    n: int
    success_rate: float
    mean_tokens: float
    tokens_per_success: float | None


class Verdict(StrEnum):
    BEATS = "BEATS"
    EQUAL = "EQUAL"
    WORSE = "WORSE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class Comparison:
    n_tasks: int
    n_orders: int
    controller: ArmSummary
    baseline: ArmSummary
    success_diff: tuple[float | None, float | None, float | None]  # point, lower, upper
    cost_ratio: tuple[float | None, float | None, float | None]
    deviation_rate: float
    exploration_rate: float
    exploration_successes_lost_per_order: float
    exploration_tokens_delta_per_order: float
    protected_deviations: int
    verdict: Verdict
    by_family: dict[str, tuple[ArmSummary, ArmSummary, float]]  # ctrl, baseline, deviation rate
    family_results: "dict[str, FamilyResult]" = field(default_factory=dict)


@dataclass(frozen=True)
class FamilyResult:
    """One family's controller-vs-rule comparison (the pre-registered per-cell acceptance unit)."""

    n_tasks: int
    controller: ArmSummary
    baseline: ArmSummary
    success_diff: tuple[float | None, float | None, float | None]
    cost_ratio: tuple[float | None, float | None, float | None]
    verdict_a2: "Verdict"  # non-inferiority margin 0.05 (M6 rule)
    verdict_cell: "Verdict"  # margin = the controller's contractual 0.10
    deviation_rate: float
    exploration_rate: float
    exploration_successes_lost_per_order: float
    exploration_tokens_delta_per_order: float


def _summary(success: Sequence[bool], tokens: Sequence[int]) -> ArmSummary:
    n = len(success)
    s = sum(success)
    tot = sum(tokens)
    return ArmSummary(
        n=n,
        success_rate=s / n if n else 0.0,
        mean_tokens=tot / n if n else 0.0,
        tokens_per_success=tot / s if s else None,
    )


def decide_verdict(
    diff: tuple[float | None, float | None, float | None],
    ratio: tuple[float | None, float | None, float | None],
    margin: float = NONINFERIORITY,
    cost_margin: float = COST_MARGIN,
) -> Verdict:
    """Frozen rule as amended (design A2). `diff`/`ratio` are (point, one-sided lower, upper).

    non-inferior: success difference lower bound >= -margin; cheaper: tokens-per-verified-success
    ratio upper bound < 1. WORSE: success confidently below -margin, or non-inferior but
    confidently more than `cost_margin` costlier. BEATS = non-inferior and cheaper; EQUAL =
    non-inferior, not cheaper, not worse; anything else is INCONCLUSIVE (never silently WORSE)."""
    _, d_lo, d_hi = diff
    _, r_lo, r_hi = ratio
    if d_lo is None or d_hi is None:
        return Verdict.INCONCLUSIVE
    noninferior = d_lo >= -margin
    cheaper = r_hi is not None and r_hi < 1.0
    if d_hi < -margin or (noninferior and r_lo is not None and r_lo > 1.0 + cost_margin):
        return Verdict.WORSE
    if noninferior and cheaper:
        return Verdict.BEATS
    if noninferior:
        return Verdict.EQUAL
    return Verdict.INCONCLUSIVE


def _inference(
    steps: Sequence[SimStep],
) -> tuple[
    tuple[float | None, float | None, float | None],
    tuple[float | None, float | None, float | None],
    int,
]:
    """Per-task paired success difference and tokens-per-success ratio (cluster bootstrap)."""
    per_task: dict[str, list[SimStep]] = defaultdict(list)
    for st in steps:
        per_task[st.task_id].append(st)
    diffs: list[float] = []
    units: list[CostUnit] = []
    for tid in sorted(per_task):
        ss = per_task[tid]
        c_succ = sum(x.success for x in ss) / len(ss)
        b_succ = sum(x.default_success for x in ss) / len(ss)
        diffs.append(c_succ - b_succ)
        units.append(
            CostUnit(
                tokens_strategy=sum(x.tokens for x in ss) / len(ss),
                verified_strategy=c_succ,
                tokens_baseline=sum(x.default_tokens for x in ss) / len(ss),
                verified_baseline=b_succ,
            )
        )
    pt, lo, hi, _ = paired_difference(diffs)
    rp, rlo, rhi, _ = cost_per_success_ratio(units)
    return (pt, lo, hi), (rp, rlo, rhi), len(per_task)


def compare(runs: Sequence[Sequence[SimStep]], margin: float = NONINFERIORITY) -> Comparison:
    """Controller vs rule baseline over several orders; the unit of inference is the task."""
    flat = [s for steps in runs for s in steps]
    diff, ratio, n_tasks = _inference(flat)
    fams: dict[str, list[SimStep]] = defaultdict(list)
    for s in flat:
        fams[s.family or "-"].append(s)
    n_orders = len(runs)
    explore = [s for s in flat if s.explored]
    family_results: dict[str, FamilyResult] = {}
    for fam, ss in sorted(fams.items()):
        f_diff, f_ratio, f_n = _inference(ss)
        f_expl = [x for x in ss if x.explored]
        family_results[fam] = FamilyResult(
            n_tasks=f_n,
            controller=_summary([x.success for x in ss], [x.tokens for x in ss]),
            baseline=_summary([x.default_success for x in ss], [x.default_tokens for x in ss]),
            success_diff=f_diff,
            cost_ratio=f_ratio,
            verdict_a2=decide_verdict(f_diff, f_ratio, margin),
            verdict_cell=decide_verdict(f_diff, f_ratio, CELL_MARGIN),
            deviation_rate=sum(x.deviated for x in ss) / len(ss),
            exploration_rate=len(f_expl) / len(ss),
            exploration_successes_lost_per_order=sum(
                int(x.default_success) - int(x.success) for x in f_expl
            )
            / max(1, n_orders),
            exploration_tokens_delta_per_order=sum(x.tokens - x.default_tokens for x in f_expl)
            / max(1, n_orders),
        )
    return Comparison(
        n_tasks=n_tasks,
        n_orders=n_orders,
        controller=_summary([s.success for s in flat], [s.tokens for s in flat]),
        baseline=_summary([s.default_success for s in flat], [s.default_tokens for s in flat]),
        success_diff=diff,
        cost_ratio=ratio,
        deviation_rate=sum(s.deviated for s in flat) / len(flat) if flat else 0.0,
        exploration_rate=len(explore) / len(flat) if flat else 0.0,
        exploration_successes_lost_per_order=sum(
            int(s.default_success) - int(s.success) for s in explore
        )
        / max(1, n_orders),
        exploration_tokens_delta_per_order=sum(s.tokens - s.default_tokens for s in explore)
        / max(1, n_orders),
        protected_deviations=sum(1 for s in flat if s.risk != "low" and s.deviated),
        verdict=decide_verdict(diff, ratio, margin),
        by_family={
            f: (
                _summary([s.success for s in ss], [s.tokens for s in ss]),
                _summary([s.default_success for s in ss], [s.default_tokens for s in ss]),
                sum(s.deviated for s in ss) / len(ss),
            )
            for f, ss in sorted(fams.items())
        },
        family_results=family_results,
    )


def evaluate(
    tasks: Sequence[TaskOutcome],
    cfg: SimConfig,
    *,
    n_orders: int = 20,
    seed: int = 20260601,
    warm: Sequence[DecisionOutcomeRecord] = (),
    horizon: int | None = None,
) -> tuple[Comparison, list[list[SimStep]]]:
    runs = [
        run_sequence(tasks, order, cfg, warm, label=f"o{k}")
        for k, order in enumerate(make_orders(len(tasks), n_orders, seed, horizon))
    ]
    return compare(runs), runs
