"""Arm runners for the capability-vs-memory experiment: B/C/D (memory) and E (compiled runtime).

All arms run through the same `TaskOrchestrator`, verifier and budgets; B-D differ from A only in
the text appended to the system prompt, E differs only in the controller and capability runtime.
"""

import hashlib
from collections.abc import Callable
from typing import Any

from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.evaluation.arms import ArmOutcome, ArmRunner
from experience_engine.execution.capability_runtime import CapabilityRuntime
from experience_engine.execution.controller import FixedPolicy, RuleCapabilityPolicy
from experience_engine.execution.orchestrator import RunResult, TaskOrchestrator
from experience_engine.memory.bank import Retrieved, TaskQuery
from experience_engine.memory.inject import MemoryAugmentedLLM
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.verifier import OutcomeVerifier


def task_query(bench: BenchTask) -> TaskQuery:
    """What retrieval may know about a task: its declared family, tools and file roles."""
    roles = {classify_path(p).value for p in bench.environment.files}
    return TaskQuery(
        family=bench.task.task_family,
        roles=frozenset(roles),
        tools=frozenset(bench.task.allowed_tools),
    )


def _outcome(result: RunResult, **extra: Any) -> ArmOutcome:
    o = result.outcome
    return ArmOutcome(
        status=o.status.value,
        verified=o.status == OutcomeStatus.VERIFIED_SUCCESS,
        tokens=o.tokens_used,
        tool_calls=o.tool_calls,
        latency_ms=o.execution_time_ms,
        termination=result.runtime.termination.value,
        **extra,
    )


def make_memory_arm(
    *,
    llm: LLMClient,
    config: AgentConfig,
    verifier: OutcomeVerifier,
    clock: Clock,
    retrieve: Callable[[TaskQuery], Retrieved],
) -> ArmRunner:
    orchestrator = TaskOrchestrator(
        controller=FixedPolicy(), verifier=verifier, clock=clock, config=config
    )

    def run(bench: BenchTask) -> ArmOutcome:
        got = retrieve(task_query(bench))
        client = MemoryAugmentedLLM(llm, got.text)
        session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
        result = orchestrator.execute(bench.task, session, client)
        return _outcome(
            result,
            retrieved_tokens=got.retrieved_tokens,
            context_tokens=client.first_prompt_tokens,
            retrieval_ms=got.retrieval_ms,
            memory_sha256=hashlib.sha256(got.text.encode()).hexdigest() if got.text else None,
        )

    return run


def make_baseline_arm(
    *, llm: LLMClient, config: AgentConfig, verifier: OutcomeVerifier, clock: Clock
) -> ArmRunner:
    """Arm A with the same bookkeeping fields as the other arms (context size recorded)."""
    return make_memory_arm(
        llm=llm,
        config=config,
        verifier=verifier,
        clock=clock,
        retrieve=lambda _q: Retrieved("", 0, 0, 0),
    )


def make_runtime_arm(
    *,
    llm: LLMClient,
    config: AgentConfig,
    verifier: OutcomeVerifier,
    clock: Clock,
    runtime: CapabilityRuntime,
) -> ArmRunner:
    """Arm E: production path (matcher -> controller -> executor -> fallback to planning)."""
    orchestrator = TaskOrchestrator(
        controller=RuleCapabilityPolicy(),
        verifier=verifier,
        clock=clock,
        config=config,
        capabilities=runtime,
    )

    def run(bench: BenchTask) -> ArmOutcome:
        session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
        result = orchestrator.execute(bench.task, session, llm)
        eligible = result.decision.feature_snapshot.get("n_eligible_capabilities", 0)
        return _outcome(
            result,
            applicable=bool(eligible),
            capability_used=result.outcome.capability_version is not None,
            fell_back=bool(result.runtime.fallback_reason),
        )

    return run
