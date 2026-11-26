"""Paired experiment grid: every arm runs on identical tasks and repetitions, concurrently, with
results returned in plan order. Pure orchestration; models, memory and capabilities are injected."""

from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.capabilities.applicability import EnvironmentFacts, check_preconditions
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.capability import CapabilityDefinition, TaskFamilyIs
from experience_engine.domain.experiment import RunRow, TransferLevel
from experience_engine.evaluation.arms import ArmOutcome, ArmRunner
from experience_engine.evaluation.engine import synthetic_negatives


@dataclass(frozen=True)
class ExperimentTask:
    bench: BenchTask
    split: str
    level: TransferLevel


def transfer_level(split: str, family: str, learned_families: Collection[str]) -> TransferLevel:
    """Derived from what discovery contained, never hand-labelled."""
    if split == "DISCOVERY":
        return "source"
    if split == "VALIDATION":
        return "intra_variant"
    return "cross_variant" if family in learned_families else "cross_family"


def tag_tasks(
    split: str, tasks: Sequence[BenchTask], learned_families: Collection[str]
) -> list[ExperimentTask]:
    return [
        ExperimentTask(t, split, transfer_level(split, t.family, learned_families)) for t in tasks
    ]


def _row(arm: str, rep: int, et: ExperimentTask, o: ArmOutcome) -> RunRow:
    return RunRow(
        arm=arm,
        task_id=et.bench.task.task_id,
        rep=rep,
        split=et.split,
        family=et.bench.family,
        variant=et.bench.variant,
        level=et.level,
        status=o.status,
        verified=o.verified,
        tokens=o.tokens,
        tool_calls=o.tool_calls,
        latency_ms=o.latency_ms,
        retrieved_tokens=o.retrieved_tokens,
        context_tokens=o.context_tokens,
        retrieval_ms=o.retrieval_ms,
        applicable=o.applicable,
        capability_used=o.capability_used,
        fell_back=o.fell_back,
        termination=o.termination,
        memory_sha256=o.memory_sha256,
        asset_kind=o.asset_kind,
        asset_ref=o.asset_ref,
        asset_family=o.asset_family,
        fallback_reason=o.fallback_reason,
        strategy=o.strategy,
    )


def run_experiment(
    arms: dict[str, ArmRunner],
    tasks: Sequence[ExperimentTask],
    *,
    repetitions: int,
    workers: int = 1,
    arm_scope: dict[str, Callable[[ExperimentTask], bool]] | None = None,
) -> list[RunRow]:
    """Run the full (task x repetition x arm) grid. A runner crash becomes an UNKNOWN row (missing
    data, excluded from comparisons) so one provider failure cannot masquerade as a failure."""
    scope = arm_scope or {}
    jobs = [
        (a, rep, et)
        for et in tasks
        for rep in range(repetitions)
        for a in arms
        if a not in scope or scope[a](et)
    ]

    def one(job: tuple[str, int, ExperimentTask]) -> RunRow:
        arm, rep, et = job
        try:
            return _row(arm, rep, et, arms[arm](et.bench))
        except Exception as exc:
            crashed = ArmOutcome(
                status="UNKNOWN",
                verified=False,
                tokens=0,
                tool_calls=0,
                latency_ms=0,
                termination=f"ERROR:{type(exc).__name__}",
            )
            return _row(arm, rep, et, crashed)

    if workers <= 1:
        return [one(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, jobs))


def applicability_matrix(
    definitions: Sequence[CapabilityDefinition], tasks: Sequence[ExperimentTask]
) -> list[dict[str, object]]:
    """Deterministic boundary check (no model calls): does each capability's applicability gate
    admit tasks of every family, and does it refuse synthetic precondition violations?"""
    out: list[dict[str, object]] = []
    for d in definitions:
        own = next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), "?")

        def admits(b: BenchTask, d: CapabilityDefinition = d) -> bool:
            session = SimulatedEnvironment(b.environment, b.task.allowed_tools)
            roles = {classify_path(p) for p in session.snapshot()}
            facts = EnvironmentFacts.of(
                b.task.task_family, b.task.allowed_tools, b.task.risk_level, roles
            )
            return not check_preconditions(d, facts)

        by_family: dict[str, list[bool]] = {}
        for et in tasks:
            by_family.setdefault(et.bench.family, []).append(admits(et.bench))
        own_tasks = [et.bench for et in tasks if et.bench.family == own]
        synthetic = synthetic_negatives(d, own_tasks)
        out.append(
            {
                "capability": f"{d.id}@{d.version}",
                "family": own,
                "admitted_by_family": {f: [sum(v), len(v)] for f, v in sorted(by_family.items())},
                "synthetic_violations_admitted": [
                    sum(admits(b) for b in synthetic),
                    len(synthetic),
                ],
            }
        )
    return out
