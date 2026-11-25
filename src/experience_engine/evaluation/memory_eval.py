"""Evaluation of a governed memory artifact with the same engine, suites and policy as capabilities.

Treatment arm `M_MEMORY_ASSET` = the baseline agent plus the artifact's text, injected only when the
artifact's typed applicability check passes. A task the artifact does not apply to is *refused*
(NOT_APPLICABLE): in the negative suite that is the correct outcome, elsewhere it counts as a
failure, exactly like a capability that cannot run.
"""

from collections.abc import Sequence

from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import AgentConfig, DatasetVersion
from experience_engine.domain.evaluation import Arm, EvaluationReport, PromotionPolicy
from experience_engine.domain.memory import MemoryDefinition
from experience_engine.evaluation.arms import ArmOutcome, ArmRunner
from experience_engine.evaluation.engine import AssetSubject, EvaluationPlan, evaluate
from experience_engine.evaluation.experiment_arms import make_memory_arm
from experience_engine.memory.applicability import memory_violations
from experience_engine.memory.bank import Retrieved
from experience_engine.memory.builders import estimate_tokens
from experience_engine.memory.runtime import memory_text
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.verifier import OutcomeVerifier


def facts_of(bench: BenchTask) -> EnvironmentFacts:
    session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
    roles = {classify_path(p) for p in session.snapshot()}
    t = bench.task
    return EnvironmentFacts.of(t.task_family, t.allowed_tools, t.risk_level, roles)


def make_memory_asset_arm(
    *,
    definition: MemoryDefinition,
    llm: LLMClient,
    config: AgentConfig,
    verifier: OutcomeVerifier,
    clock: Clock,
) -> ArmRunner:
    text = memory_text(definition)
    tokens = estimate_tokens(text)
    inner = make_memory_arm(
        llm=llm,
        config=config,
        verifier=verifier,
        clock=clock,
        retrieve=lambda _q: Retrieved(text, 1, tokens, 0),
    )

    def run(bench: BenchTask) -> ArmOutcome:
        reasons = memory_violations(definition, facts_of(bench))
        if reasons:
            return ArmOutcome(
                status="NOT_APPLICABLE",
                verified=False,
                tokens=0,
                tool_calls=0,
                latency_ms=0,
                applicable=False,
                termination="NOT_APPLICABLE:" + reasons[0][:80],
            )
        out = inner(bench)
        return ArmOutcome(**{**out.__dict__, "applicable": True})

    return run


def evaluate_memory(
    *,
    definition: MemoryDefinition,
    policy: PromotionPolicy,
    clock: Clock,
    baseline: ArmRunner,
    memory_arm: ArmRunner,
    source_tasks: Sequence[BenchTask],
    heldout_tasks: Sequence[BenchTask],
    transfer_tasks: Sequence[BenchTask],
    other_family_tasks: Sequence[BenchTask],
    datasets: dict[str, DatasetVersion],
    verifier_version: str,
    model_settings: dict[str, object],
    seeds: list[int],
    workers: int = 1,
    learning_tokens: int = 0,
    learning_wall_ms: int = 0,
) -> EvaluationReport:
    plan = EvaluationPlan(
        tenant_id=definition.tenant_id,
        definition=AssetSubject(
            asset_id=definition.memory_id,
            version=definition.version,
            preconditions=list(definition.applicability_conditions),
            family=definition.task_family,
        ),
        policy=policy,
        clock=clock,
        arms={Arm.BASELINE: baseline, Arm.MEMORY_ASSET: memory_arm},
        source_tasks=source_tasks,
        heldout_tasks=heldout_tasks,
        transfer_tasks=transfer_tasks,
        other_family_tasks=other_family_tasks,
        datasets={k: v for k, v in datasets.items()},
        verifier_version=verifier_version,
        model_settings=model_settings,
        seeds=seeds,
        workers=workers,
        treatment=Arm.MEMORY_ASSET,
        asset_kind="MEMORY",
        learning_tokens=learning_tokens,
        learning_wall_ms=learning_wall_ms,
    )
    return evaluate(plan)
