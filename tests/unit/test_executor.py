"""Executor tests: correctness on a real simulated environment, plus limits and isolation."""

import copy
from collections.abc import Mapping
from typing import Any

import pytest
from test_capability_dsl import good

from experience_engine.adapters.llm.fake import HeuristicDecisionProvider
from experience_engine.bench.dataset import BenchTask, generate_split
from experience_engine.bench.environment import EnvironmentSpec, SimulatedEnvironment
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.capabilities.applicability import EnvironmentFacts, check_preconditions
from experience_engine.capabilities.executor import (
    CancellationToken,
    CapabilityExecutor,
    ExecReason,
    ExecStatus,
)
from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.benchmark import Split
from experience_engine.domain.capability import CapabilityDefinition, OutType
from experience_engine.domain.outcome import OutcomeStatus, aggregate_status
from experience_engine.ports.decisions import DecisionError, DecisionResult
from experience_engine.ports.tools import ToolResult
from support import FakeClock, first_task

TENANT = "bench"


def definition(**changes: Any) -> CapabilityDefinition:
    d = copy.deepcopy(good())
    d.update(changes)
    return CapabilityDefinition.model_validate(d)


class SpyTools:
    """Wraps a session gateway and records every call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def describe(self):  # type: ignore[no-untyped-def]
        return self._inner.describe()

    def call(self, tool: str, args: Mapping[str, object]) -> ToolResult:
        self.calls.append((tool, dict(args)))
        return self._inner.call(tool, args)


class Rig:
    def __init__(self, task: BenchTask | None = None, decisions: Any = None, clock: Any = None):
        self.task = task or first_task()
        self.session = SimulatedEnvironment(self.task.environment, self.task.task.allowed_tools)
        self.tools = SpyTools(self.session.tools)
        self.clock = clock or FakeClock()
        self.verifier = SimulationVerifier()
        self.decisions = decisions or HeuristicDecisionProvider()

    def verify(self) -> bool | None:
        s = aggregate_status(self.verifier.verify(self.task.task, dict(self.session.snapshot())))
        return None if s == OutcomeStatus.UNKNOWN else s == OutcomeStatus.VERIFIED_SUCCESS

    def facts(self) -> EnvironmentFacts:
        roles = {classify_path(p) for p in self.session.snapshot()}
        t = self.task.task
        return EnvironmentFacts.of(t.task_family, t.allowed_tools, t.risk_level, roles)

    def run(self, d: CapabilityDefinition, tenant: str = TENANT, cancel: Any = None):  # type: ignore[no-untyped-def]
        ex = CapabilityExecutor(
            tools=self.tools,
            decisions=self.decisions,
            clock=self.clock,
            verify=self.verify,  # type: ignore[arg-type]
        )
        return ex.run(
            d, capability_tenant=tenant, task=self.task.task, facts=self.facts(), cancel=cancel
        )


def test_executes_the_procedure_and_the_environment_is_independently_fixed() -> None:
    rig = Rig()
    res = rig.run(definition())
    assert (res.status, res.reason) == (ExecStatus.COMPLETED, ExecReason.VERIFIED)
    assert [c[0] for c in rig.tools.calls] == ["list_files", "read_file", "set_json_value"]
    assert res.tool_calls == 3 and res.tokens_used > 0 and res.verified_by_capability
    # the executor's own claim is irrelevant: the environment state is what the verifier checks
    assert rig.verify() is True


def test_works_across_config_layouts_via_role_selection() -> None:
    psycopg = next(t for t in generate_split(Split.DISCOVERY) if t.variant == "psycopg_pool")
    rig = Rig(psycopg)
    assert rig.run(definition()).status == ExecStatus.COMPLETED
    assert rig.tools.calls[1][1] == {"path": "app/settings.json"}  # not hard-coded to one path


def test_preconditions_gate_execution_without_running_anything() -> None:
    other_family = next(t for t in generate_split(Split.DISCOVERY) if t.family == "timeout")
    rig = Rig(other_family)
    res = rig.run(definition())
    assert res.status == ExecStatus.NOT_APPLICABLE and rig.tools.calls == []
    assert "task_family" in res.detail

    base = first_task()
    no_tool = base.task.model_copy(update={"allowed_tools": ["list_files", "read_file"]})
    rig2 = Rig(BenchTask(task=no_tool, environment=base.environment, family="pool", variant="x"))
    assert rig2.run(definition()).status == ExecStatus.NOT_APPLICABLE

    files = {p: c for p, c in base.environment.files.items() if not p.startswith("app/")}
    rig3 = Rig(
        BenchTask(
            task=base.task, environment=EnvironmentSpec(files=files), family="pool", variant="x"
        )
    )
    res3 = rig3.run(definition())
    assert res3.status == ExecStatus.NOT_APPLICABLE and "config" in res3.detail
    assert check_preconditions(definition(), rig3.facts())


def test_tenant_boundary_aborts_before_any_tool_call() -> None:
    rig = Rig()
    res = rig.run(definition(), tenant="another-tenant")
    assert (res.status, res.reason) == (ExecStatus.ABORTED, ExecReason.TENANT_MISMATCH)
    assert rig.tools.calls == []


def test_tampered_definitions_are_revalidated_and_never_executed() -> None:
    rig = Rig()
    valid = definition()
    steps = list(valid.steps)
    bad_step = steps[0].model_copy(update={"tool": "write_file"})  # bypasses the schema via copy
    tampered = valid.model_copy(update={"steps": [bad_step, *steps[1:]]})
    res = rig.run(tampered)
    assert (res.status, res.reason) == (ExecStatus.ABORTED, ExecReason.INVALID_DEFINITION)
    assert rig.tools.calls == []
    unsafe = valid.model_copy(update={"objective": "eval(payload)"})
    assert rig.run(unsafe).reason == ExecReason.INVALID_DEFINITION and rig.tools.calls == []


def test_permissions_and_task_allowlist_limit_which_tools_can_run() -> None:
    rig = Rig()
    d = definition()
    # a definition whose permissions omit a tool it uses is statically invalid -> aborted
    narrowed = d.model_copy(update={"permissions": ["list_files", "read_file"]})
    assert rig.run(narrowed).reason == ExecReason.INVALID_DEFINITION and rig.tools.calls == []


def _retry_definition(max_steps: int, attempts: int = 5) -> CapabilityDefinition:
    return definition(
        steps=[
            {
                "op": "TOOL_CALL",
                "id": "list_files",
                "tool": "list_files",
                "args": {},
                "bind": "files",
            },
            {
                "op": "RETRY",
                "id": "again",
                "max_attempts": attempts,
                "retry_on": "tool_failed",
                "steps": [
                    {
                        "op": "TOOL_CALL",
                        "id": "read_missing",
                        "tool": "read_file",
                        "args": {"path": {"literal": "missing.json"}},
                        "bind": "x",
                    }
                ],
            },
            {"op": "VERIFY", "id": "verify_outcome", "verifier": "outcome_verifier"},
        ],
        budgets={"max_steps": max_steps, "max_tokens": 4000, "timeout_ms": 60000},
    )


def test_step_budget_is_enforced_on_executed_steps_including_retries() -> None:
    rig = Rig()
    res = rig.run(_retry_definition(max_steps=5))
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.MAX_STEPS)
    assert res.steps_executed == 5 and len(rig.tools.calls) == 4  # list + 3 failed reads


def test_retry_is_bounded_and_reports_exhaustion() -> None:
    rig = Rig()
    res = rig.run(_retry_definition(max_steps=40, attempts=2))
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.RETRY_EXHAUSTED)
    assert len(rig.tools.calls) == 3  # list + exactly two attempts


class FixedDecisions:
    def __init__(self, values: dict[str, Any] | Exception, tokens: int = 50) -> None:
        self.values, self.tokens, self.calls = values, tokens, 0

    def decide(
        self,
        *,
        template_id: str,
        objective: str,
        inputs: Mapping[str, str],
        output_schema: Mapping[str, OutType],
        max_tokens: int,
    ) -> DecisionResult:
        self.calls += 1
        if isinstance(self.values, Exception):
            raise self.values
        return DecisionResult(values=dict(self.values), tokens=self.tokens)


def test_token_budget_stops_before_the_mutating_step() -> None:
    d = definition(budgets={"max_steps": 12, "max_tokens": 100, "timeout_ms": 60000})
    rig = Rig(decisions=FixedDecisions({"key": "database.pool_size", "value": 20}, tokens=500))
    res = rig.run(d)
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.MAX_TOKENS)
    assert "set_json_value" not in [c[0] for c in rig.tools.calls]
    assert rig.verify() is False  # environment untouched


def test_wall_clock_timeout() -> None:
    class Ticking(FakeClock):
        def monotonic_ms(self) -> int:
            self.ms += 40_000
            return self.ms

    rig = Rig(clock=Ticking())
    res = rig.run(definition())
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.TIMEOUT)
    assert len(rig.tools.calls) < 3


def test_cancellation_aborts_and_stops_further_tool_calls() -> None:
    token = CancellationToken()

    class CancelOnDecide(FixedDecisions):
        def decide(self, **kw: Any) -> DecisionResult:
            token.cancel()
            return super().decide(**kw)

    rig = Rig(decisions=CancelOnDecide({"key": "database.pool_size", "value": 20}))
    res = rig.run(definition(), cancel=token)
    assert (res.status, res.reason) == (ExecStatus.ABORTED, ExecReason.CANCELLED)
    assert [c[0] for c in rig.tools.calls] == [
        "list_files",
        "read_file",
    ]  # no mutation after cancel


@pytest.mark.parametrize(
    "values",
    [
        {"key": "database.pool_size"},  # missing field
        {"key": "database.pool_size", "value": 20, "extra": 1},  # extra field
        {"key": "database.pool_size", "value": "twenty"},  # wrong type
        {"key": 5, "value": 20},  # wrong type
        {"key": "database.pool_size", "value": True},  # bool is not a number
    ],
)
def test_malformed_decisions_never_reach_a_tool(values: dict[str, Any]) -> None:
    rig = Rig(decisions=FixedDecisions(values))
    res = rig.run(definition())
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.DECISION_FAILED)
    assert "set_json_value" not in [c[0] for c in rig.tools.calls]


def test_decision_provider_failure_falls_back_safely() -> None:
    rig = Rig(decisions=FixedDecisions(DecisionError("model down")))
    res = rig.run(definition())
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.DECISION_FAILED)


def test_ambiguous_path_roles_refuse_to_guess() -> None:
    base = first_task()
    files = dict(base.environment.files)
    files["app/other.json"] = files["app/config.json"]  # two config-role files
    rig = Rig(
        BenchTask(
            task=base.task, environment=EnvironmentSpec(files=files), family="pool", variant="x"
        )
    )
    res = rig.run(definition())
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.AMBIGUOUS_PATH)
    assert [c[0] for c in rig.tools.calls] == ["list_files"]


def test_failed_verification_is_reported_not_hidden() -> None:
    rig = Rig(decisions=FixedDecisions({"key": "database.pool_size", "value": 1}))
    res = rig.run(definition())
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.VERIFY_FAILED)
    assert not res.verified_by_capability and rig.verify() is False


def test_tool_failure_without_retry_falls_back() -> None:
    d = definition(
        steps=[
            {
                "op": "TOOL_CALL",
                "id": "list_files",
                "tool": "list_files",
                "args": {},
                "bind": "files",
            },
            {
                "op": "TOOL_CALL",
                "id": "read_missing",
                "tool": "read_file",
                "args": {"path": {"literal": "missing.json"}},
            },
            {"op": "VERIFY", "id": "verify_outcome", "verifier": "outcome_verifier"},
        ],
    )
    res = Rig().run(d)
    assert (res.status, res.reason) == (ExecStatus.FALLBACK, ExecReason.TOOL_FAILED)


def test_assert_condition_and_human_approval_steps() -> None:
    cond = {"left": {"var": "n"}, "operator": "gt", "right": {"literal": 1}}
    base_steps = [
        {"op": "TOOL_CALL", "id": "list_files", "tool": "list_files", "args": {}, "bind": "files"},
    ]
    exists = {
        "op": "ASSERT",
        "id": "has_files",
        "message_id": "no_files",
        "condition": {"left": {"var": "files"}, "operator": "exists"},
    }
    assert (
        Rig()
        .run(
            definition(
                steps=[
                    *base_steps,
                    exists,
                    {"op": "VERIFY", "id": "v", "verifier": "outcome_verifier"},
                ]
            )
        )
        .reason
        == ExecReason.VERIFY_FAILED
    )  # assert passed; nothing was fixed so verification fails
    del cond
    missing = {
        "op": "ASSERT",
        "id": "needs_x",
        "message_id": "no_x",
        "condition": {
            "left": {"var": "files"},
            "operator": "in",
            "right": {"literal": "nope.json"},
        },
    }
    assert Rig().run(definition(steps=[*base_steps, missing])).reason == ExecReason.ASSERT_FAILED
    approval = {"op": "HUMAN_APPROVAL", "id": "ask", "reason_id": "risky_change"}
    res = Rig().run(definition(steps=[*base_steps, approval]))
    assert (res.status, res.reason) == (ExecStatus.ESCALATE, ExecReason.HUMAN_APPROVAL_REQUIRED)


def test_two_environments_never_share_state() -> None:
    a, b = Rig(), Rig()
    a.run(definition())
    assert a.verify() is True and b.verify() is False  # b's environment is untouched
    assert b.tools.calls == []


def test_executor_source_has_no_dynamic_execution_primitives() -> None:
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/experience_engine/capabilities/executor.py"
    banned_calls = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "open"}
    banned_modules = {"subprocess", "os", "socket", "importlib", "ctypes", "pickle"}
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned_calls
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & banned_modules
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned_modules
