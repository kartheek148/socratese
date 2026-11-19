from collections.abc import Callable, Sequence

from experience_engine import RUNTIME_VERSION
from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import ENVIRONMENT_VERSION, SimulatedEnvironment
from experience_engine.domain.benchmark import AgentConfig, BenchmarkRunRecord, DatasetVersion
from experience_engine.domain.redaction import Redactor
from experience_engine.execution.controller import Controller
from experience_engine.execution.orchestrator import TaskOrchestrator
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.persistence import RunLifecycle
from experience_engine.ports.store import ResultStore
from experience_engine.ports.verifier import OutcomeVerifier

LLMFactory = Callable[[BenchTask, int], LLMClient]


def run_benchmark(
    tasks: Sequence[BenchTask],
    *,
    dataset: DatasetVersion,
    seed: int,
    config: AgentConfig,
    llm_factory: LLMFactory,
    controller: Controller,
    verifier: OutcomeVerifier,
    clock: Clock,
    store: ResultStore,
    lifecycle: RunLifecycle | None = None,
    artifacts: ArtifactStore | None = None,
    redactor: Redactor | None = None,
) -> list[BenchmarkRunRecord]:
    """Run each task in a fresh session (new environment, new LLM client, no carried state).

    This is the no-learning baseline path: nothing is read from or written to any memory.
    Optional lifecycle/artifacts additionally persist traces, outcomes and outbox events (M2);
    they never influence execution.
    """
    orchestrator = TaskOrchestrator(
        controller=controller,
        verifier=verifier,
        clock=clock,
        config=config,
        lifecycle=lifecycle,
        artifacts=artifacts,
        redactor=redactor,
    )
    records: list[BenchmarkRunRecord] = []
    for bench_task in tasks:
        task = bench_task.task
        session = SimulatedEnvironment(bench_task.environment, task.allowed_tools)
        result = orchestrator.execute(task, session, llm_factory(bench_task, seed))
        record = BenchmarkRunRecord(
            tenant_id=task.tenant_id,
            run_id=result.run_id,
            correlation_id=result.run_id,
            dataset=dataset,
            seed=seed,
            config=config,
            runtime_version=RUNTIME_VERSION,
            verifier_version=verifier.version,
            environment_version=ENVIRONMENT_VERSION,
            tags={
                "family": bench_task.family,
                "variant": bench_task.variant,
                "task_id": task.task_id,
            },
            decision=result.decision,
            outcome=result.outcome,
            termination=result.runtime.termination,
            agent_claimed_done=result.runtime.agent_claimed_done,
            steps=result.runtime.steps,
            started_at=result.started_at,
            ended_at=result.ended_at,
        )
        store.save_run(record)
        records.append(record)
    return records
