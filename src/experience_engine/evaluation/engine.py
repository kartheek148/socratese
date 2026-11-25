"""Evaluation engine: runs the suites and arms and produces an EvaluationReport + verdict.

Suites: SOURCE_REPLAY (necessary, never sufficient), HELD_OUT, TRANSFER, NEGATIVE (the capability
must refuse), REGRESSION (tasks the baseline already solves must still be solved). Controls:
A fixed baseline, B memory-only hint. Lifecycle cost charges the capability for learning and for
the control-arm tokens spent evaluating it.
"""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from experience_engine import RUNTIME_VERSION
from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import ENVIRONMENT_VERSION, EnvironmentSpec
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import DatasetVersion
from experience_engine.domain.capability import (
    CapabilityDefinition,
    FileRolePresent,
    Precondition,
    RiskLevelAtMost,
    TaskFamilyIs,
    ToolsAvailable,
)
from experience_engine.domain.evaluation import (
    Arm,
    ArmMetrics,
    EvaluationReport,
    LifecycleCost,
    PromotionPolicy,
    Suite,
    TaskResult,
)
from experience_engine.domain.stats import wilson_interval
from experience_engine.evaluation.arms import CURRENT_SEED, ArmOutcome, ArmRunner
from experience_engine.evaluation.policy import decide_verdict, pooled, success_lower_bound
from experience_engine.ports.clock import Clock

EVALUATOR_VERSION = "evaluator/1"
MAX_SYNTHETIC_NEGATIVES = 4


class _HasPreconditions(Protocol):
    @property
    def preconditions(self) -> Sequence[Precondition]: ...


@dataclass(frozen=True)
class AssetSubject:
    """What the engine needs to know about a governed asset that is not a capability."""

    asset_id: str
    version: int
    preconditions: list[Precondition]
    family: str | None = None  # the family under evaluation even if the asset does not declare one


@dataclass
class EvaluationPlan:
    tenant_id: str
    definition: CapabilityDefinition | AssetSubject
    policy: PromotionPolicy
    clock: Clock
    arms: dict[Arm, ArmRunner]
    source_tasks: Sequence[BenchTask]
    heldout_tasks: Sequence[BenchTask]
    transfer_tasks: Sequence[BenchTask]
    other_family_tasks: Sequence[BenchTask]
    datasets: dict[str, DatasetVersion]
    verifier_version: str
    model_settings: dict[str, object]
    seeds: list[int]  # every task is run once per seed (independent repetitions)
    treatment: Arm = Arm.CAPABILITY  # the gated arm (a capability, or a governed memory asset)
    asset_kind: str = "CAPABILITY"
    workers: int = 1  # concurrent runs; arms and tasks are stateless, results keep plan order
    learning_tokens: int = 0
    learning_wall_ms: int = 0
    max_negative_other_family: int = 4
    results: list[TaskResult] = field(default_factory=list)


def _metrics(
    outcomes: Sequence[ArmOutcome], capability_arm: bool, n_tasks: int | None = None
) -> ArmMetrics:
    n = len(outcomes)
    ok = sum(1 for o in outcomes if o.verified)
    tokens = sum(o.tokens for o in outcomes)
    calls = sum(o.tool_calls for o in outcomes)
    lo, hi = wilson_interval(ok, n)
    return ArmMetrics(
        n=n,
        n_tasks=n_tasks,
        verified=ok,
        rate=ok / n if n else 0.0,
        ci_low=lo,
        ci_high=hi,
        tokens_total=tokens,
        tool_calls_total=calls,
        latency_mean_ms=sum(o.latency_ms for o in outcomes) / n if n else 0.0,
        tokens_per_success=tokens / ok if ok else None,
        tool_calls_per_success=calls / ok if ok else None,
        applicable=sum(1 for o in outcomes if o.applicable) if capability_arm else None,
        fell_back=(
            sum(
                1
                for o in outcomes
                if o.termination and not o.termination.endswith("VERIFIED") and o.applicable
            )
            if capability_arm
            else None
        ),
    )


def synthetic_negatives(
    definition: _HasPreconditions, tasks: Sequence[BenchTask]
) -> list[BenchTask]:
    """Environments that violate a precondition the definition declares (missing tool, missing
    file role). The capability must refuse each of them. One case per violation kind, rotating
    across base tasks so several layouts are exercised."""
    if not tasks:
        return []
    required_tools = sorted(
        {t for p in definition.preconditions if isinstance(p, ToolsAvailable) for t in p.tools}
    )
    roles = [p.role for p in definition.preconditions if isinstance(p, FileRolePresent)]
    kinds: list[tuple[str, str]] = [("tool", t) for t in required_tools] + [
        ("role", r.value) for r in roles
    ]
    out: list[BenchTask] = []
    for i, (kind, name) in enumerate(kinds[:MAX_SYNTHETIC_NEGATIVES]):
        base = tasks[i % len(tasks)]
        task_id = f"{base.task.task_id}::no_{name}"
        if kind == "tool":
            allowed = [t for t in base.task.allowed_tools if t != name]
            task = base.task.model_copy(update={"task_id": task_id, "allowed_tools": allowed})
            env = base.environment
        else:
            files = {
                p: c for p, c in base.environment.files.items() if classify_path(p).value != name
            }
            task = base.task.model_copy(update={"task_id": task_id})
            env = EnvironmentSpec(files=files)
        out.append(BenchTask(task=task, environment=env, family=base.family, variant=base.variant))
    return out


def evaluate(plan: EvaluationPlan) -> EvaluationReport:
    started = plan.clock.now()
    d = (
        plan.definition
        if isinstance(plan.definition, AssetSubject)
        else AssetSubject(
            plan.definition.id, plan.definition.version, plan.definition.preconditions
        )
    )
    family = d.family or next(
        (p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), ""
    )
    risk = next((p.level for p in d.preconditions if isinstance(p, RiskLevelAtMost)), "high")

    suite_tasks: dict[Suite, Sequence[BenchTask]] = {
        Suite.SOURCE_REPLAY: [t for t in plan.source_tasks if t.family == family],
        Suite.HELD_OUT: [t for t in plan.heldout_tasks if t.family == family],
        Suite.TRANSFER: [t for t in plan.transfer_tasks if t.family == family],
    }
    seeds = plan.seeds or [0]
    results: dict[tuple[Suite, Arm, str, int], ArmOutcome] = {}
    suites: dict[str, dict[str, ArmMetrics]] = {}
    rows: list[TaskResult] = []

    def run_all(
        jobs: Sequence[tuple[Suite, Arm, BenchTask, int]],
    ) -> list[ArmOutcome]:
        def one(job: tuple[Suite, Arm, BenchTask, int]) -> ArmOutcome:
            token = CURRENT_SEED.set(job[3])
            try:
                return plan.arms[job[1]](job[2])
            finally:
                CURRENT_SEED.reset(token)

        if plan.workers <= 1:
            return [one(j) for j in jobs]
        with ThreadPoolExecutor(max_workers=plan.workers) as pool:
            return list(pool.map(one, jobs))

    def record(suite: Suite, arm: Arm, t: BenchTask, o: ArmOutcome, seed: int) -> None:
        results[(suite, arm, t.task.task_id, seed)] = o
        rows.append(
            TaskResult(
                suite=suite,
                arm=arm,
                task_id=t.task.task_id,
                family=t.family,
                variant=t.variant,
                status=o.status,
                tokens=o.tokens,
                tool_calls=o.tool_calls,
                latency_ms=o.latency_ms,
                applicable=o.applicable,
                termination=o.termination,
                seed=seed,
            )
        )

    for suite, tasks in suite_tasks.items():
        jobs = [(suite, arm, t, sd) for arm in plan.arms for t in tasks for sd in seeds]
        per_arm: dict[str, list[ArmOutcome]] = {arm.value: [] for arm in plan.arms}
        for job, o in zip(jobs, run_all(jobs), strict=True):
            record(job[0], job[1], job[2], o, job[3])
            per_arm[job[1].value].append(o)
        suites[suite.value] = {
            a: _metrics(o, capability_arm=(a == plan.treatment.value), n_tasks=len(tasks))
            for a, o in per_arm.items()
        }

    # Negative applicability: the capability must refuse other families and precondition violations.
    others = list(plan.other_family_tasks)
    others = [t for t in others if t.family != family][: plan.max_negative_other_family]
    negatives = others + synthetic_negatives(d, suite_tasks[Suite.HELD_OUT])
    # refusals are decided before any step runs, so one pass (seed 0) is enough
    neg_jobs = [(Suite.NEGATIVE, plan.treatment, t, seeds[0]) for t in negatives]
    neg_outs = run_all(neg_jobs)
    for job, o in zip(neg_jobs, neg_outs, strict=True):
        record(job[0], job[1], job[2], o, job[3])
    suites[Suite.NEGATIVE.value] = {
        plan.treatment.value: _metrics(neg_outs, True, n_tasks=len(negatives))
    }
    # for the negative suite "verified" means a correct refusal
    refusals = sum(1 for o in neg_outs if o.applicable is False)
    base_neg = suites[Suite.NEGATIVE.value][plan.treatment.value]
    suites[Suite.NEGATIVE.value][plan.treatment.value] = base_neg.model_copy(
        update={
            "verified": refusals,
            "rate": refusals / len(neg_outs) if neg_outs else 0.0,
        }
    )
    false_app = (len(neg_outs) - refusals) / len(neg_outs) if neg_outs else None

    # Regression: tasks the baseline already solves must still be solved by the capability.
    regressions = 0
    reg_cap: list[ArmOutcome] = []
    for suite in (Suite.SOURCE_REPLAY, Suite.HELD_OUT, Suite.TRANSFER):
        for t in suite_tasks[suite]:
            for sd in seeds:
                base = results.get((suite, Arm.BASELINE, t.task.task_id, sd))
                cap = results.get((suite, plan.treatment, t.task.task_id, sd))
                if base and cap and base.verified:
                    reg_cap.append(cap)
                    regressions += int(not cap.verified)
    suites[Suite.REGRESSION.value] = {plan.treatment.value: _metrics(reg_cap, True)}

    # Lifecycle cost: learning + control-arm tokens spent evaluating C + C's own execution tokens.
    scored = (Suite.SOURCE_REPLAY, Suite.HELD_OUT, Suite.TRANSFER)
    _, _, tok_a, _ = pooled(suites, Arm.BASELINE, scored)
    _, _, tok_b, _ = pooled(suites, Arm.MEMORY, scored)
    _, ok_c, tok_c, _ = pooled(suites, plan.treatment, scored)
    _, ok_a, tok_a_all, _ = pooled(suites, Arm.BASELINE, scored)
    overhead = tok_a + tok_b
    lifecycle = LifecycleCost(
        learning_tokens=plan.learning_tokens,
        learning_wall_ms=plan.learning_wall_ms,
        evaluation_overhead_tokens=overhead,
        capability_exec_tokens=tok_c,
        capability_verified=ok_c,
        lifecycle_tokens_per_success=(
            (plan.learning_tokens + overhead + tok_c) / ok_c if ok_c else None
        ),
        baseline_tokens_per_success=(tok_a_all / ok_a) if ok_a else None,
    )

    verdict, reasons = decide_verdict(
        plan.policy,
        suites,
        false_applicability_rate=false_app,
        regressions=regressions,
        treatment=plan.treatment,
    )
    return EvaluationReport(
        capability_id=d.asset_id,
        asset_kind=plan.asset_kind,
        treatment_arm=plan.treatment.value,
        version=d.version,
        tenant_id=plan.tenant_id,
        risk_class=risk,
        policy_name=plan.policy.name,
        policy_hash=plan.policy.policy_hash(),
        policy=plan.policy,
        verdict=verdict,
        reasons=reasons,
        suites=suites,
        false_applicability_rate=false_app,
        regressions=regressions,
        success_lower_bound=success_lower_bound(suites, plan.treatment),
        lifecycle=lifecycle,
        dataset_versions={
            s: {"name": v.name, "version": v.version, "content_hash": v.content_hash}
            for s, v in plan.datasets.items()
        },
        model_settings=plan.model_settings,
        seeds=seeds,
        runtime_version=RUNTIME_VERSION,
        verifier_version=plan.verifier_version,
        environment_version=ENVIRONMENT_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        started_at=started,
        completed_at=plan.clock.now(),
        task_results=rows,
    )
