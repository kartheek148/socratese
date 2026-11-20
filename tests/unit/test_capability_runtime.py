"""M5 unit tests: matching, controller, composition and orchestrated capability execution."""

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_capability_dsl import good

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.llm.fake import HeuristicDecisionProvider, HeuristicFakeLLM
from experience_engine.bench.environment import SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.applicability import EnvironmentFacts
from experience_engine.capabilities.executor import CapabilityExecutor, ExecReason, ExecStatus
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import AgentConfig, ModelSettings
from experience_engine.domain.capability import CapabilityDefinition
from experience_engine.domain.decision import Strategy
from experience_engine.domain.experience import ExclusionReason, classify_eligibility
from experience_engine.domain.outcome import OutcomeStatus, TerminationReason
from experience_engine.domain.trace import TraceEventType
from experience_engine.execution.capability_runtime import CapabilityRuntime
from experience_engine.execution.controller import (
    SELECTED_CAPABILITY_KEY,
    ControllerInput,
    RuleCapabilityPolicy,
)
from experience_engine.execution.matcher import (
    CapabilityMatcher,
    EligibleCapability,
    MatchResult,
    ScoredCandidate,
    check_eligibility,
)
from experience_engine.execution.orchestrator import TaskOrchestrator
from experience_engine.ports.decisions import DecisionError, DecisionResult
from experience_engine.ports.tools import ToolResult
from support import FakeClock, InMemoryLifecycle, ScriptedLLM, first_task

TENANT = "bench"
POOL = "pool.0123456789"


def make(**changes: Any) -> CapabilityDefinition:
    d = copy.deepcopy(good())
    d.update(changes)
    return CapabilityDefinition.model_validate(d)


def wrapper(
    cap_id: str, dep: str, *, permissions: list[str] | None = None, max_steps: int = 12
) -> CapabilityDefinition:
    """A capability whose only work is delegating to a declared dependency."""
    return make(
        id=cap_id,
        dependencies=[dep],
        permissions=permissions or ["list_files", "read_file", "set_json_value"],
        budgets={"max_steps": max_steps, "max_tokens": 4000, "timeout_ms": 60000},
        steps=[
            {"op": "SUBCAPABILITY", "id": "delegate", "capability": dep, "args": {}},
            {"op": "VERIFY", "id": "verify_outcome", "verifier": "outcome_verifier"},
        ],
    )


class MemoryCatalog:
    """ExecutableCapabilityCatalog fake: serves exactly what it was given, per tenant."""

    def __init__(self, definitions: list[CapabilityDefinition], tenant: str = TENANT) -> None:
        self._tenant = tenant
        self._defs = {f"{d.id}@{d.version}": d for d in definitions}

    def list_executable(self, tenant_id: str) -> list[CapabilityDefinition]:
        return list(self._defs.values()) if tenant_id == self._tenant else []

    def get_executable(
        self, tenant_id: str, capability_id: str, version: int
    ) -> CapabilityDefinition | None:
        return self._defs.get(f"{capability_id}@{version}") if tenant_id == self._tenant else None


class FixedRetriever:
    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores

    def rank(self, task: Any, candidates: Any) -> list[ScoredCandidate]:
        out = [ScoredCandidate(d, self._scores.get(d.id, 0.0)) for d in candidates]
        return sorted(out, key=lambda s: -s.similarity)


def facts_for(bench: Any) -> EnvironmentFacts:
    t = bench.task
    session = SimulatedEnvironment(bench.environment, t.allowed_tools)
    roles = {classify_path(p) for p in session.snapshot()}
    return EnvironmentFacts.of(t.task_family, t.allowed_tools, t.risk_level, roles)


# ---- matcher ------------------------------------------------------------------------------------
def test_matching_capability_is_eligible() -> None:
    bench = first_task()
    res = CapabilityMatcher(MemoryCatalog([make()])).match(bench.task, facts_for(bench))
    assert [e.ref for e in res.eligible] == [f"{POOL}@1"] and not res.rejected


def test_similarity_never_grants_eligibility() -> None:
    """A wrong-family capability that retrieval ranks first (0.99) is still rejected."""
    bench = first_task()  # pool task
    wrong = make(
        id="timeout.9999999999",
        preconditions=[
            {"kind": "task_family_is", "family": "timeout"},
            {"kind": "tools_available", "tools": ["list_files", "read_file", "set_json_value"]},
        ],
    )
    matcher = CapabilityMatcher(
        MemoryCatalog([wrong, make()]), FixedRetriever({"timeout.9999999999": 0.99, POOL: 0.01})
    )
    res = matcher.match(bench.task, facts_for(bench))
    assert [e.ref for e in res.eligible] == [f"{POOL}@1"]  # the low-similarity right one
    assert res.rejected[0].capability_id == "timeout.9999999999"
    assert res.rejected[0].similarity == 0.99
    assert any("family" in r for r in res.rejected[0].reasons)


def test_a_perfect_similarity_score_cannot_bypass_the_gate() -> None:
    bench = first_task()
    wrong = make(
        id="timeout.9999999999",
        preconditions=[{"kind": "task_family_is", "family": "timeout"}],
    )
    res = CapabilityMatcher(MemoryCatalog([wrong]), FixedRetriever({wrong.id: 1.0})).match(
        bench.task, facts_for(bench)
    )
    assert res.eligible == [] and len(res.rejected) == 1


def test_permissions_budgets_and_tools_are_checked_against_the_task() -> None:
    bench = first_task()
    facts = facts_for(bench)
    cat = MemoryCatalog([])
    narrow = bench.task.model_copy(update={"allowed_tools": ["list_files", "read_file"]})
    assert any("permissions" in r for r in check_eligibility(make(), narrow, facts, cat))
    small = bench.task.model_copy(update={"max_steps": 5, "max_tokens": 100, "deadline_ms": 10})
    reasons = " | ".join(check_eligibility(make(), small, facts, cat))
    assert "steps" in reasons and "tokens" in reasons and "deadline" in reasons
    few_calls = bench.task.model_copy(update={"max_tool_calls": 2})
    assert any("tool calls" in r for r in check_eligibility(make(), few_calls, facts, cat))


def test_tampered_definitions_are_ineligible() -> None:
    bench = first_task()
    tampered = make().model_copy(update={"objective": "eval(payload)"})
    reasons = check_eligibility(tampered, bench.task, facts_for(bench), MemoryCatalog([]))
    assert reasons and "static validation" in reasons[0]


def test_dependencies_must_be_executable_acyclic_and_not_widen_permissions() -> None:
    bench = first_task()
    facts = facts_for(bench)
    parent = wrapper("wrap.0000000001", f"{POOL}@1")
    # dependency absent from the catalog (e.g. not ACTIVE)
    absent = check_eligibility(parent, bench.task, facts, MemoryCatalog([parent]))
    assert any("not an executable version" in r for r in absent)
    assert check_eligibility(parent, bench.task, facts, MemoryCatalog([parent, make()])) == []
    # the child needs a tool the parent does not declare
    narrow = wrapper("wrap.0000000001", f"{POOL}@1", permissions=["list_files", "read_file"])
    widened = check_eligibility(narrow, bench.task, facts, MemoryCatalog([narrow, make()]))
    assert any("parent does not declare" in r for r in widened)
    # a cycle A -> B -> A
    a = wrapper("cyc.000000000a", "cyc.000000000b@1")
    b = wrapper("cyc.000000000b", "cyc.000000000a@1")
    cyc = check_eligibility(a, bench.task, facts, MemoryCatalog([a, b]))
    assert any("cycle" in r for r in cyc)


def test_other_tenants_capabilities_are_invisible() -> None:
    bench = first_task()
    res = CapabilityMatcher(MemoryCatalog([make()], tenant="other")).match(
        bench.task, facts_for(bench)
    )
    assert res.n_catalog == 0 and not res.eligible and not res.rejected


# ---- controller ---------------------------------------------------------------------------------
def decide(match: MatchResult | None) -> Any:
    bench = first_task()
    return RuleCapabilityPolicy().decide(
        ControllerInput(task=bench.task, run_id="r", decision_id="d", match=match)
    )


def eligible(d: CapabilityDefinition, similarity: float) -> EligibleCapability:
    return EligibleCapability(d, similarity)


def test_controller_plans_from_scratch_when_nothing_is_eligible() -> None:
    d = decide(MatchResult(n_catalog=2))
    assert d.selected_action == Strategy.GENERAL_PLANNING
    assert d.eligible_actions == [Strategy.GENERAL_PLANNING]
    assert d.policy_version == "rule-capability/1" and d.selection_probability == 1.0
    assert SELECTED_CAPABILITY_KEY not in d.feature_snapshot
    assert decide(None).selected_action == Strategy.GENERAL_PLANNING


def test_controller_selects_best_eligible_and_records_the_decision_context() -> None:
    low, high = make(id="pool.00000000aa"), make(id="pool.00000000bb")
    match = MatchResult(n_catalog=2, eligible=[eligible(low, 0.2), eligible(high, 0.7)])
    d = decide(match)
    assert d.selected_action == Strategy.DIRECT_CAPABILITY
    assert d.eligible_actions == [Strategy.DIRECT_CAPABILITY, Strategy.GENERAL_PLANNING]
    assert d.feature_snapshot[SELECTED_CAPABILITY_KEY] == "pool.00000000bb@1"
    assert d.estimates["similarity:pool.00000000aa@1"] == 0.2
    assert "pool.00000000bb@1" in d.reason
    assert len(d.feature_snapshot["candidates"]) == 2  # type: ignore[arg-type]


def test_controller_composes_when_the_choice_has_dependencies() -> None:
    d = decide(MatchResult(n_catalog=1, eligible=[eligible(wrapper("w.0000000001", "x@1"), 0.5)]))
    assert d.selected_action == Strategy.COMPOSE_CAPABILITIES


# ---- composition (executor) ---------------------------------------------------------------------
class Rig:
    def __init__(self, catalog: MemoryCatalog, clock: FakeClock | None = None) -> None:
        self.bench = first_task()
        self.session = SimulatedEnvironment(self.bench.environment, self.bench.task.allowed_tools)
        self.catalog = catalog
        self.clock = clock or FakeClock()
        self.verifier = SimulationVerifier()

    def verify(self) -> bool | None:
        from experience_engine.domain.outcome import aggregate_status

        s = aggregate_status(self.verifier.verify(self.bench.task, dict(self.session.snapshot())))
        return None if s == OutcomeStatus.UNKNOWN else s == OutcomeStatus.VERIFIED_SUCCESS

    def run(self, d: CapabilityDefinition, resolver: Any = "catalog") -> Any:
        if resolver == "catalog":
            resolver = lambda ref: self.catalog.get_executable(  # noqa: E731
                TENANT, ref.split("@")[0], int(ref.split("@")[1])
            )
        ex = CapabilityExecutor(
            tools=self.session.tools,
            decisions=HeuristicDecisionProvider(),
            clock=self.clock,
            verify=self.verify,
            resolver=resolver,
        )
        return ex.run(
            d, capability_tenant=TENANT, task=self.bench.task, facts=facts_for(self.bench)
        )


def test_composed_capability_runs_its_dependency_and_fixes_the_environment() -> None:
    parent = wrapper("wrap.0000000001", f"{POOL}@1")
    rig = Rig(MemoryCatalog([parent, make()]))
    res = rig.run(parent)
    assert (res.status, res.reason) == (ExecStatus.COMPLETED, ExecReason.VERIFIED)
    assert res.tool_calls == 3 and rig.verify() is True


def test_child_cannot_exceed_the_parents_step_budget() -> None:
    parent = wrapper("wrap.0000000001", f"{POOL}@1", max_steps=3)  # child alone needs 5 steps
    res = Rig(MemoryCatalog([parent, make()])).run(parent)
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.MAX_STEPS)


def test_child_tokens_count_against_the_parents_budget() -> None:
    parent = wrapper("wrap.0000000001", f"{POOL}@1").model_copy(
        update={"budgets": make().budgets.model_copy(update={"max_tokens": 5})}
    )
    res = Rig(MemoryCatalog([parent, make()])).run(parent)
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.MAX_TOKENS)


def test_unavailable_dependency_is_refused_without_side_effects() -> None:
    parent = wrapper("wrap.0000000001", f"{POOL}@1")
    rig = Rig(MemoryCatalog([parent]))  # the dependency is not in the executable catalog
    res = rig.run(parent)
    assert res.reason == ExecReason.DEPENDENCY_UNAVAILABLE
    assert res.tool_calls == 0 and rig.verify() is False


def test_composition_requires_a_resolver_and_a_declared_dependency() -> None:
    parent = wrapper("wrap.0000000001", f"{POOL}@1")
    assert Rig(MemoryCatalog([parent, make()])).run(parent, resolver=None).reason == (
        ExecReason.DEPENDENCY_UNAVAILABLE
    )
    undeclared = parent.model_copy(update={"dependencies": []})
    res = Rig(MemoryCatalog([undeclared, make()])).run(undeclared)
    assert (res.status, res.reason) == (ExecStatus.ABORTED, ExecReason.INVALID_DEFINITION)
    assert res.tool_calls == 0


def test_child_with_wider_permissions_than_its_parent_is_refused() -> None:
    parent = wrapper("wrap.0000000001", f"{POOL}@1", permissions=["list_files", "read_file"])
    res = Rig(MemoryCatalog([parent, make()])).run(parent)
    assert (res.status, res.reason) == (ExecStatus.ABORTED, ExecReason.TOOL_NOT_ALLOWED)
    assert res.tool_calls == 0


def test_cycles_and_deep_nesting_are_stopped() -> None:
    a = wrapper("cyc.000000000a", "cyc.000000000b@1")
    b = wrapper("cyc.000000000b", "cyc.000000000a@1")
    assert Rig(MemoryCatalog([a, b])).run(a).reason == ExecReason.COMPOSITION_LIMIT
    chain = [wrapper(f"deep.00000000{i:02d}", f"deep.00000000{i + 1:02d}@1") for i in range(5)]
    chain.append(make(id="deep.0000000005"))
    res = Rig(MemoryCatalog(chain)).run(chain[0])
    assert res.reason == ExecReason.COMPOSITION_LIMIT and res.tool_calls == 0


# ---- orchestrated runs --------------------------------------------------------------------------
class FailingDecisions:
    def decide(self, **_: Any) -> DecisionResult:
        raise DecisionError("model unavailable")


class SlowFailingDecisions:
    """Burns the whole deadline, then fails."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def decide(self, **_: Any) -> DecisionResult:
        self._clock.ms += 10_000_000
        raise DecisionError("too slow")


def build(
    definitions: list[CapabilityDefinition],
    clock: FakeClock,
    decisions: Any = None,
    tmp_path: Path | None = None,
    experimental: bool = False,
) -> tuple[TaskOrchestrator, InMemoryLifecycle]:
    catalog = MemoryCatalog(definitions)
    runtime = CapabilityRuntime(
        catalog=catalog,
        matcher=CapabilityMatcher(catalog),
        decisions_for=lambda _llm: decisions or HeuristicDecisionProvider(),
        experimental=experimental,
    )
    lifecycle = InMemoryLifecycle()
    orch = TaskOrchestrator(
        controller=RuleCapabilityPolicy(),
        verifier=SimulationVerifier(),
        clock=clock,
        config=AgentConfig(
            config_id="C_CAPABILITIES",
            prompt_version="t",
            model=ModelSettings(provider="fake", model="m"),
        ),
        lifecycle=lifecycle,  # type: ignore[arg-type]
        artifacts=LocalFsArtifactStore(tmp_path or Path("/tmp/ee-unit-artifacts")),
        capabilities=runtime,
    )
    return orch, lifecycle


def events(lifecycle: InMemoryLifecycle, kind: TraceEventType) -> list[Any]:
    return [e for e in lifecycle.events if e.type == kind]  # type: ignore[attr-defined]


def test_capability_serves_the_task_and_is_traced(tmp_path: Path) -> None:
    clock = FakeClock()
    orch, lc = build([make()], clock, tmp_path=tmp_path)
    llm = ScriptedLLM([])  # general planning must not be needed
    result = orch.execute(
        first_task().task,
        SimulatedEnvironment(first_task().environment, first_task().task.allowed_tools),
        llm,
    )  # noqa: E501
    o = result.outcome
    assert o.status == OutcomeStatus.VERIFIED_SUCCESS and llm.calls == 0
    assert o.capability_version == f"{POOL}@1" and o.config_id == "C_CAPABILITIES"
    assert result.runtime.termination == TerminationReason.CAPABILITY_COMPLETED
    assert o.tool_calls == 3 and o.tokens_used > 0
    assert result.decision.selected_action == Strategy.DIRECT_CAPABILITY
    assert result.decision.outcome_status == OutcomeStatus.VERIFIED_SUCCESS
    kinds = [e.type for e in lc.events]  # type: ignore[attr-defined]
    for kind in (
        TraceEventType.DECISION,
        TraceEventType.CAPABILITY_MATCH,
        TraceEventType.CAPABILITY_EXEC,
        TraceEventType.VERIFICATION,
    ):
        assert kind in kinds
    tool_events = events(lc, TraceEventType.TOOL_CALL)
    assert [e.action for e in tool_events] == ["list_files", "read_file", "set_json_value"]
    assert {e.component for e in tool_events} == {"capability_tool_gateway"}
    assert len(lc.decisions) == 1  # type: ignore[arg-type]


def test_useless_capability_fix_is_caught_by_the_verifier_then_falls_back(tmp_path: Path) -> None:
    class Useless(HeuristicDecisionProvider):
        def decide(self, **kw: Any) -> DecisionResult:
            res = super().decide(**kw)
            return DecisionResult({**res.values, "value": 1}, res.tokens)

    bench = first_task()
    orch, _ = build([make()], FakeClock(), decisions=Useless(), tmp_path=tmp_path)
    # the fallback planner claims success without changing anything: still not a success
    result = orch.execute(
        bench.task,
        SimulatedEnvironment(bench.environment, bench.task.allowed_tools),
        ScriptedLLM([{"final": "done"}]),
    )
    assert result.runtime.fallback_reason and "VERIFY_FAILED" in result.runtime.fallback_reason
    assert result.runtime.agent_claimed_done
    assert result.outcome.status == OutcomeStatus.FAILURE


def test_failed_capability_falls_back_to_general_planning_within_remaining_budget(
    tmp_path: Path,
) -> None:
    bench = first_task()
    orch, lc = build([make()], FakeClock(), decisions=FailingDecisions(), tmp_path=tmp_path)
    result = orch.execute(
        bench.task,
        SimulatedEnvironment(bench.environment, bench.task.allowed_tools),
        HeuristicFakeLLM(),
    )
    assert result.runtime.fallback_reason and "DECISION_FAILED" in result.runtime.fallback_reason
    assert result.outcome.status == OutcomeStatus.VERIFIED_SUCCESS  # general planning solved it
    assert result.outcome.capability_version == f"{POOL}@1"
    fb = events(lc, TraceEventType.FALLBACK)
    assert len(fb) == 1 and fb[0].status == "DECISION_FAILED"
    assert fb[0].metrics["remaining_deadline_ms"] <= bench.task.deadline_ms
    assert result.outcome.failure_reasons[0].startswith(f"{POOL}@1: DECISION_FAILED")


def test_fallback_never_resets_the_budget(tmp_path: Path) -> None:
    clock = FakeClock()
    bench = first_task()
    orch, _ = build([make()], clock, decisions=SlowFailingDecisions(clock), tmp_path=tmp_path)
    llm = ScriptedLLM([])
    result = orch.execute(
        bench.task, SimulatedEnvironment(bench.environment, bench.task.allowed_tools), llm
    )
    assert llm.calls == 0  # the deadline was already spent
    assert result.runtime.termination == TerminationReason.DEADLINE
    assert result.outcome.status != OutcomeStatus.VERIFIED_SUCCESS


def test_fallback_gets_only_the_budget_the_capability_left(tmp_path: Path) -> None:
    class Useless(HeuristicDecisionProvider):
        def decide(self, **kw: Any) -> DecisionResult:
            res = super().decide(**kw)
            return DecisionResult({**res.values, "value": 1}, res.tokens)

    bench = first_task()
    task = bench.task.model_copy(update={"max_tool_calls": 5})  # capability uses 3 of them
    orch, _ = build([make()], FakeClock(), decisions=Useless(), tmp_path=tmp_path)
    llm = ScriptedLLM([{"tool": "list_files", "args": {}}] * 10)
    result = orch.execute(
        task, SimulatedEnvironment(bench.environment, bench.task.allowed_tools), llm
    )
    assert result.runtime.termination == TerminationReason.MAX_TOOL_CALLS
    assert result.outcome.tool_calls == 5  # 3 by the capability + only the 2 that remained


def test_structural_problem_aborts_without_fallback(tmp_path: Path) -> None:
    class BypassMatcher:
        """Deliberately broken gate: proves the executor re-validates independently."""

        def match(self, task: Any, facts: Any) -> MatchResult:
            bad = make().model_copy(update={"objective": "eval(payload)"})
            return MatchResult(n_catalog=1, eligible=[EligibleCapability(bad, 1.0)])

    bench = first_task()
    orch, lc = build([make()], FakeClock(), tmp_path=tmp_path)
    runtime = orch._capabilities
    assert runtime is not None
    orch._capabilities = CapabilityRuntime(
        catalog=runtime.catalog,
        matcher=BypassMatcher(),  # type: ignore[arg-type]
        decisions_for=runtime.decisions_for,
    )
    llm = ScriptedLLM([])
    result = orch.execute(
        bench.task, SimulatedEnvironment(bench.environment, bench.task.allowed_tools), llm
    )
    assert result.runtime.termination == TerminationReason.CAPABILITY_ABORTED
    assert llm.calls == 0 and result.outcome.tool_calls == 0
    assert result.outcome.status != OutcomeStatus.VERIFIED_SUCCESS
    assert not events(lc, TraceEventType.FALLBACK)


def test_human_approval_step_escalates(tmp_path: Path) -> None:
    approval = make(
        steps=[
            {"op": "TOOL_CALL", "id": "list_files", "tool": "list_files", "args": {}, "bind": "f"},
            {"op": "HUMAN_APPROVAL", "id": "ask", "reason_id": "risky_change"},
        ]
    )
    bench = first_task()
    orch, _ = build([approval], FakeClock(), tmp_path=tmp_path)
    result = orch.execute(
        bench.task,
        SimulatedEnvironment(bench.environment, bench.task.allowed_tools),
        ScriptedLLM([]),
    )
    assert result.runtime.termination == TerminationReason.ESCALATED
    assert result.outcome.human_interventions == 1
    assert result.outcome.capability_version == f"{approval.id}@1"


def test_controller_cannot_select_a_capability_the_gate_did_not_pass(tmp_path: Path) -> None:
    class Rogue:
        policy_version = "rogue/1"

        def decide(self, ctx: ControllerInput) -> Any:
            d = RuleCapabilityPolicy().decide(ctx)
            return d.model_copy(
                update={
                    "selected_action": Strategy.DIRECT_CAPABILITY,
                    "eligible_actions": [Strategy.DIRECT_CAPABILITY],
                    "feature_snapshot": {SELECTED_CAPABILITY_KEY: "timeout.9999999999@1"},
                }
            )

    bench = first_task()
    orch, _ = build([], FakeClock(), tmp_path=tmp_path)
    orch._controller = Rogue()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="ineligible"):
        orch.execute(
            bench.task,
            SimulatedEnvironment(bench.environment, bench.task.allowed_tools),
            ScriptedLLM([]),
        )


def test_no_eligible_capability_runs_exactly_like_the_baseline(tmp_path: Path) -> None:
    bench = first_task()
    orch, _ = build([], FakeClock(), tmp_path=tmp_path)
    result = orch.execute(
        bench.task,
        SimulatedEnvironment(bench.environment, bench.task.allowed_tools),
        HeuristicFakeLLM(),
    )
    assert result.decision.selected_action == Strategy.GENERAL_PLANNING
    assert result.outcome.capability_version is None
    assert result.outcome.status == OutcomeStatus.VERIFIED_SUCCESS


def test_experimental_catalog_tags_the_run(tmp_path: Path) -> None:
    bench = first_task()
    orch, lc = build([make()], FakeClock(), tmp_path=tmp_path, experimental=True)
    orch.execute(
        bench.task,
        SimulatedEnvironment(bench.environment, bench.task.allowed_tools),
        ScriptedLLM([]),
    )
    assert lc.runs[0].tags == {"capability_catalog": "experimental"}  # type: ignore[attr-defined]


def test_capability_assisted_runs_are_excluded_from_learning() -> None:
    assert classify_eligibility(OutcomeStatus.VERIFIED_SUCCESS, "DISCOVERY", True, True) == (
        False,
        ExclusionReason.CAPABILITY_ASSISTED,
    )
    assert classify_eligibility(OutcomeStatus.VERIFIED_SUCCESS, "DISCOVERY", True, False) == (
        True,
        None,
    )


def test_tool_denials_are_traced_and_reraised() -> None:
    from experience_engine.execution.capability_runtime import TracingToolGateway
    from experience_engine.ports.tools import ToolNotAllowedError

    class Deny:
        def describe(self) -> list[Any]:
            return []

        def call(self, tool: str, args: Mapping[str, object]) -> ToolResult:
            raise ToolNotAllowedError(tool)

    class Sink:
        def __init__(self) -> None:
            self.specs: list[Any] = []

        def emit(self, spec: Any) -> str:
            self.specs.append(spec)
            return f"e{len(self.specs)}"

    sink = Sink()
    gw = TracingToolGateway(Deny(), sink, "root")
    with pytest.raises(ToolNotAllowedError):
        gw.call("rm", {"path": "/"})
    assert sink.specs[0].status == "denied" and sink.specs[0].parent_event_id == "root"
    assert gw.last_event_id == "e1"
