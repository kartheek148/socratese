"""Arm runners for the evaluation: A baseline agent, B baseline + episodic hint (memory-only
control), C compiled capability. All arms run in a fresh simulated environment and are scored by
the same independent verifier from final environment state, never from their own claims.
"""

from collections.abc import Callable
from dataclasses import dataclass

from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.capabilities.executor import (
    CapabilityExecutor,
    ExecStatus,
)
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.capability import CapabilityDefinition
from experience_engine.domain.experience import Trajectory
from experience_engine.domain.outcome import OutcomeStatus, aggregate_status
from experience_engine.execution.controller import FixedPolicy
from experience_engine.execution.orchestrator import TaskOrchestrator
from experience_engine.ports.clock import Clock
from experience_engine.ports.decisions import DecisionProvider
from experience_engine.ports.llm import ChatMessage, LLMClient, LLMRequest, LLMResponse
from experience_engine.ports.verifier import OutcomeVerifier


@dataclass(frozen=True)
class ArmOutcome:
    status: str
    verified: bool
    tokens: int
    tool_calls: int
    latency_ms: int
    applicable: bool | None = None
    termination: str | None = None
    # memory / runtime arms (capability-vs-memory experiment)
    retrieved_tokens: int = 0
    context_tokens: int = 0
    retrieval_ms: int = 0
    fell_back: bool | None = None
    capability_used: bool | None = None
    memory_sha256: str | None = None


ArmRunner = Callable[[BenchTask], ArmOutcome]


class HintedLLM:
    """Memory-only control: the baseline agent plus one retrieved example in its system prompt."""

    def __init__(self, inner: LLMClient, hint: str):
        self._inner = inner
        self._hint = hint

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = list(request.messages)
        if messages and messages[0].role == "system":
            merged = f"{messages[0].content}\n\nMemory (a previous similar task): {self._hint}"
            messages[0] = ChatMessage(role="system", content=merged)
        return self._inner.complete(request.model_copy(update={"messages": messages}))


def episodic_hint(trajectory: Trajectory) -> str:
    steps = []
    for s in trajectory.steps:
        if s.kind != "tool":
            continue
        steps.append(f"{s.tool}({s.role.value})" if s.role is not None else str(s.tool))
    return f"it was solved by these tool steps in order: {', '.join(steps)}."


def make_agent_arm(
    *,
    llm: LLMClient,
    config: AgentConfig,
    verifier: OutcomeVerifier,
    clock: Clock,
    hint_for: Callable[[BenchTask], str | None] | None = None,
) -> ArmRunner:
    orchestrator = TaskOrchestrator(
        controller=FixedPolicy(), verifier=verifier, clock=clock, config=config
    )

    def run(bench_task: BenchTask) -> ArmOutcome:
        task = bench_task.task
        session = SimulatedEnvironment(bench_task.environment, task.allowed_tools)
        hint = hint_for(bench_task) if hint_for else None
        client: LLMClient = HintedLLM(llm, hint) if hint else llm
        result = orchestrator.execute(task, session, client)
        o = result.outcome
        return ArmOutcome(
            status=o.status.value,
            verified=o.status == OutcomeStatus.VERIFIED_SUCCESS,
            tokens=o.tokens_used,
            tool_calls=o.tool_calls,
            latency_ms=o.execution_time_ms,
            termination=result.runtime.termination.value,
        )

    return run


def make_capability_arm(
    *,
    definition: CapabilityDefinition,
    tenant_id: str,
    decisions: DecisionProvider,
    verifier: OutcomeVerifier,
    clock: Clock,
) -> ArmRunner:
    def run(bench_task: BenchTask) -> ArmOutcome:
        task = bench_task.task
        session = SimulatedEnvironment(bench_task.environment, task.allowed_tools)
        roles = {classify_path(p) for p in session.snapshot()}
        facts = EnvironmentFacts.of(task.task_family, task.allowed_tools, task.risk_level, roles)

        def verify() -> bool | None:
            status = aggregate_status(verifier.verify(task, dict(session.snapshot())))
            if status == OutcomeStatus.UNKNOWN:
                return None
            return status == OutcomeStatus.VERIFIED_SUCCESS

        executor = CapabilityExecutor(
            tools=session.tools, decisions=decisions, clock=clock, verify=verify
        )
        t0 = clock.monotonic_ms()
        result = executor.run(definition, capability_tenant=tenant_id, task=task, facts=facts)
        latency = clock.monotonic_ms() - t0
        if result.status == ExecStatus.NOT_APPLICABLE:
            return ArmOutcome(
                status="NOT_APPLICABLE",
                verified=False,
                tokens=0,
                tool_calls=0,
                latency_ms=latency,
                applicable=False,
                termination=result.reason.value,
            )
        # Independent verification of the final environment, whatever the capability claims.
        final = aggregate_status(verifier.verify(task, dict(session.snapshot())))
        return ArmOutcome(
            status=final.value,
            verified=final == OutcomeStatus.VERIFIED_SUCCESS,
            tokens=result.tokens_used,
            tool_calls=result.tool_calls,
            latency_ms=latency,
            applicable=True,
            termination=f"{result.status.value}:{result.reason.value}",
        )

    return run
