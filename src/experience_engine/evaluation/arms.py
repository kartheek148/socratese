"""Arm runners for the evaluation: A baseline agent, B baseline + episodic hint (memory-only
control), C compiled capability. All arms run in a fresh simulated environment and are scored by
the same independent verifier from final environment state, never from their own claims.
"""

import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from experience_engine.evaluation.baseline_cache import BaselineStore


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
    asset_kind: str | None = None
    asset_ref: str | None = None
    asset_family: str | None = None
    fallback_reason: str | None = None
    # resource accounting (M5.9)
    cache_hit: bool = False  # served from a recorded run, not executed now
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


ArmRunner = Callable[[BenchTask], ArmOutcome]

# The repetition (seed) currently being executed. The engine sets it around every runner call so
# wrappers such as `SharedArm` can tell repetitions of one task apart without changing ArmRunner.
CURRENT_SEED: ContextVar[int] = ContextVar("evaluation_seed", default=0)
# Content hash of the dataset the task being run belongs to (part of the baseline cache key).
CURRENT_DATASET: ContextVar[str] = ContextVar("evaluation_dataset", default="")


class SharedArm:
    """Computes each (task, repetition) once and serves it to every evaluation that asks.

    Used for the control arm when several assets are evaluated in one session: they are then
    compared against the *same* baseline draws (paired), at the cost of one baseline run. With a
    `store` the draws also persist across sessions (see baseline_cache). Reuse is flagged
    (`cache_hit`) so reports can separate executed runs from reused ones.
    """

    def __init__(self, inner: ArmRunner, store: "BaselineStore | None" = None):
        self._inner = inner
        self._store = store
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, int], threading.Lock] = {}
        self._done: dict[tuple[str, int], ArmOutcome] = {}
        self.computed = 0
        self.served = 0

    def __call__(self, bench: BenchTask) -> ArmOutcome:
        seed = CURRENT_SEED.get()
        key = (bench.task.task_id, seed)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            self.served += 1
            if key in self._done:
                return replace(self._done[key], cache_hit=True)
            disk_key = self._store.key(bench, seed, CURRENT_DATASET.get()) if self._store else None
            if self._store is not None and disk_key is not None:
                cached = self._store.get(disk_key)
                if cached is not None:
                    self._done[key] = cached
                    return replace(cached, cache_hit=True)
            outcome = self._inner(bench)
            self._done[key] = outcome
            self.computed += 1
            if self._store is not None and disk_key is not None:
                self._store.put(disk_key, outcome)
            return outcome


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
            prompt_tokens=result.runtime.prompt_tokens,
            completion_tokens=result.runtime.completion_tokens,
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
