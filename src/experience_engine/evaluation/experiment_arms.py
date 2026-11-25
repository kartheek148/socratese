"""Arm runners for the capability-vs-memory experiment: B/C/D (memory) and E (compiled runtime).

All arms run through the same `TaskOrchestrator`, verifier and budgets; B-D differ from A only in
the text appended to the system prompt, E differs only in the controller and capability runtime.
"""

import hashlib
from collections.abc import Callable
from typing import Any

from experience_engine.bench.dataset import BenchTask
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import AgentConfig
from experience_engine.domain.capability import TaskFamilyIs
from experience_engine.domain.memory import MemoryType
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.evaluation.arms import ArmOutcome, ArmRunner
from experience_engine.execution.capability_runtime import CapabilityRuntime
from experience_engine.execution.controller import FixedPolicy, RuleCapabilityPolicy
from experience_engine.execution.orchestrator import RunResult, TaskOrchestrator
from experience_engine.memory.bank import Retrieved, TaskQuery
from experience_engine.memory.inject import MemoryAugmentedLLM
from experience_engine.memory.runtime import select_memory
from experience_engine.ports.clock import Clock
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.memory import ActiveMemoryCatalog
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


def facts_for(bench: BenchTask) -> EnvironmentFacts:
    session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
    roles = {classify_path(p) for p in session.snapshot()}
    t = bench.task
    return EnvironmentFacts.of(t.task_family, t.allowed_tools, t.risk_level, roles)


def make_gated_memory_arm(
    *,
    llm: LLMClient,
    config: AgentConfig,
    verifier: OutcomeVerifier,
    clock: Clock,
    catalog: ActiveMemoryCatalog,
    tenant_id: str,
    memory_type: MemoryType,
) -> ArmRunner:
    """Production-mode memory: only ACTIVE versions of `memory_type`, applicability-checked. If no
    applicable ACTIVE asset exists the task runs under general planning and the reason is recorded.
    """
    orchestrator = TaskOrchestrator(
        controller=FixedPolicy(), verifier=verifier, clock=clock, config=config
    )

    def run(bench: BenchTask) -> ArmOutcome:
        sel = select_memory(catalog, tenant_id, memory_type, facts_for(bench))
        client = MemoryAugmentedLLM(llm, sel.text)
        session = SimulatedEnvironment(bench.environment, bench.task.allowed_tools)
        result = orchestrator.execute(bench.task, session, client)
        a = sel.artifact
        return _outcome(
            result,
            applicable=a is not None,
            retrieved_tokens=sel.retrieved_tokens,
            context_tokens=client.first_prompt_tokens,
            retrieval_ms=sel.retrieval_ms,
            memory_sha256=hashlib.sha256(sel.text.encode()).hexdigest() if sel.text else None,
            asset_kind="MEMORY" if a else None,
            asset_ref=sel.ref,
            asset_family=a.task_family if a else None,
            fallback_reason=sel.fallback_reason,
        )

    return run


def family_of_ref(runtime: CapabilityRuntime, tenant_id: str, ref: str | None) -> str | None:
    if ref is None:
        return None
    cid, _, ver = ref.partition("@")
    for d in runtime.catalog.list_executable(tenant_id):
        if d.id == cid and str(d.version) == ver:
            return next((p.family for p in d.preconditions if isinstance(p, TaskFamilyIs)), None)
    return None


def make_runtime_arm(
    *,
    llm: LLMClient,
    config: AgentConfig,
    verifier: OutcomeVerifier,
    clock: Clock,
    runtime: CapabilityRuntime,
    tenant_id: str = "bench",
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
        ref = result.outcome.capability_version
        n_catalog = result.decision.feature_snapshot.get("n_catalog", 0)
        reason = result.runtime.fallback_reason
        if ref is None:
            reason = f"no eligible ACTIVE capability (catalog size {n_catalog})"
        return _outcome(
            result,
            applicable=bool(eligible),
            capability_used=ref is not None,
            fell_back=bool(result.runtime.fallback_reason),
            asset_kind="CAPABILITY" if ref else None,
            asset_ref=ref,
            asset_family=family_of_ref(runtime, tenant_id, ref),
            fallback_reason=reason,
        )

    return run
