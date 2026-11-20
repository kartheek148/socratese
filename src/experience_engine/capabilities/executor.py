"""Sandboxed interpreter for the capability DSL.

Safety model (engineering rule 7): this interpreter can only perform the allow-listed DSL
operations. It has no eval/exec/import/subprocess, resolves values only through structured
references, and every tool call goes through the injected `ToolGateway`, which is already scoped
to one task environment and tenant. Limits enforced on every step: maximum executed steps, token
budget, wall-clock timeout, cancellation, tool allowlist (definition permissions ∩ task allowed
tools ∩ approved capability tools), and a tenant check. The definition is re-validated here even
though it was validated on registration (defense in depth against tampering).

It does NOT decide whether the task succeeded: a VERIFY step calls an injected verifier, and the
caller (evaluator / runtime) must still verify the final environment independently.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from experience_engine.capabilities.applicability import EnvironmentFacts, check_preconditions
from experience_engine.capabilities.tools import APPROVED_TOOLS, classify_path
from experience_engine.capabilities.validator import CapabilityValidationError, validate_definition
from experience_engine.domain.capability import (
    Assert,
    CapabilityDefinition,
    Cond,
    Condition,
    Expr,
    HumanApproval,
    InputRef,
    Literal_,
    LlmDecision,
    Operator,
    OutType,
    Retry,
    SelectPath,
    Step,
    Subcapability,
    ToolCall,
    VarRef,
    Verify,
)
from experience_engine.domain.task import TaskSpec
from experience_engine.ports.clock import Clock
from experience_engine.ports.decisions import DecisionError, DecisionProvider
from experience_engine.ports.tools import ToolGateway, ToolNotAllowedError


class ExecStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    FALLBACK = "FALLBACK"  # safe to hand over to general planning
    ABORTED = "ABORTED"  # structural/safety problem: do not continue
    ESCALATE = "ESCALATE"  # needs a human


class ExecReason(StrEnum):
    VERIFIED = "VERIFIED"
    NO_VERIFY_STEP = "NO_VERIFY_STEP"
    PRECONDITION_VIOLATED = "PRECONDITION_VIOLATED"
    MAX_STEPS = "MAX_STEPS"
    MAX_TOKENS = "MAX_TOKENS"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    TOOL_FAILED = "TOOL_FAILED"
    INVALID_ARGS = "INVALID_ARGS"
    AMBIGUOUS_PATH = "AMBIGUOUS_PATH"
    UNBOUND = "UNBOUND"
    DECISION_FAILED = "DECISION_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    VERIFY_UNKNOWN = "VERIFY_UNKNOWN"
    ASSERT_FAILED = "ASSERT_FAILED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    INVALID_DEFINITION = "INVALID_DEFINITION"
    UNSUPPORTED_STEP = "UNSUPPORTED_STEP"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    COMPOSITION_LIMIT = "COMPOSITION_LIMIT"


_ABORT_REASONS = {
    ExecReason.TOOL_NOT_ALLOWED,
    ExecReason.TENANT_MISMATCH,
    ExecReason.INVALID_DEFINITION,
    ExecReason.CANCELLED,
}


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass(frozen=True)
class StepLog:
    op: str
    step_id: str
    ok: bool
    tokens: int = 0
    note: str = ""


@dataclass
class ExecutionResult:
    status: ExecStatus
    reason: ExecReason
    detail: str = ""
    steps: list[StepLog] = field(default_factory=list)
    steps_executed: int = 0
    tokens_used: int = 0
    tool_calls: int = 0
    verified_by_capability: bool = False


class _Stop(Exception):
    def __init__(self, reason: ExecReason, detail: str = "", status: ExecStatus | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status = status or (
            ExecStatus.ABORTED if reason in _ABORT_REASONS else ExecStatus.FALLBACK
        )


class _Retryable(Exception):
    def __init__(self, kind: str, stop: _Stop):
        super().__init__(kind)
        self.kind = kind
        self.stop = stop


Value = Any  # JSON-like runtime values: str | int | float | bool | list[str] | dict


class CapabilityExecutor:
    def __init__(
        self,
        *,
        tools: ToolGateway,
        decisions: DecisionProvider,
        clock: Clock,
        verify: Callable[[], bool | None],
        resolver: Callable[[str], CapabilityDefinition | None] | None = None,
    ):
        self._tools = tools
        self._decisions = decisions
        self._clock = clock
        self._verify = verify
        # resolves "id@version" to an EXECUTABLE (ACTIVE) definition; None disables composition
        self._resolver = resolver

    # ---- public -------------------------------------------------------------------------------
    def run(
        self,
        definition: CapabilityDefinition,
        *,
        capability_tenant: str,
        task: TaskSpec,
        facts: EnvironmentFacts,
        inputs: Mapping[str, Value] | None = None,
        cancel: CancellationToken | None = None,
    ) -> ExecutionResult:
        result = ExecutionResult(ExecStatus.COMPLETED, ExecReason.NO_VERIFY_STEP)
        try:
            # declared dependencies are resolved (ACTIVE-only) when a SUBCAPABILITY step runs
            validate_definition(definition, known_dependencies=definition.dependencies)
        except CapabilityValidationError as exc:
            return self._stop(result, _Stop(ExecReason.INVALID_DEFINITION, str(exc)[:200]))
        if capability_tenant != task.tenant_id:
            return self._stop(
                result, _Stop(ExecReason.TENANT_MISMATCH, "capability tenant differs")
            )
        violations = check_preconditions(definition, facts)
        if violations:
            result.status, result.reason = (
                ExecStatus.NOT_APPLICABLE,
                ExecReason.PRECONDITION_VIOLATED,
            )
            result.detail = "; ".join(violations)[:300]
            return result

        run = _Run(
            executor=self,
            definition=definition,
            task=task,
            facts=facts,
            allowed=set(definition.permissions) & set(facts.allowed_tools) & set(APPROVED_TOOLS),
            inputs=dict(inputs or {}),
            cancel=cancel,
            result=result,
            start_ms=self._clock.monotonic_ms(),
            stack=(definition.id,),
        )
        try:
            run.exec_steps(definition.steps, depth=1)
        except _Stop as stop:
            return self._stop(result, stop)
        except _Retryable as retryable:  # not inside a RETRY step: a plain failure
            return self._stop(result, retryable.stop)
        if result.verified_by_capability:
            result.reason = ExecReason.VERIFIED
        return result

    @staticmethod
    def _stop(result: ExecutionResult, stop: _Stop) -> ExecutionResult:
        result.status, result.reason, result.detail = stop.status, stop.reason, stop.detail[:300]
        return result


@dataclass
class _Run:
    executor: CapabilityExecutor
    definition: CapabilityDefinition
    task: TaskSpec
    facts: EnvironmentFacts
    allowed: set[str]
    inputs: dict[str, Value]
    cancel: CancellationToken | None
    result: ExecutionResult
    start_ms: int
    stack: tuple[str, ...]
    parent: "_Run | None" = None
    env: dict[str, Value] = field(default_factory=dict)

    def _chain(self) -> list[CapabilityDefinition]:
        """This definition and every ancestor: a child runs inside ALL their budgets."""
        out: list[CapabilityDefinition] = []
        cur: _Run | None = self
        while cur is not None:
            out.append(cur.definition)
            cur = cur.parent
        return out

    # ---- limits -------------------------------------------------------------------------------
    def _tick(self) -> None:
        chain = self._chain()
        max_steps = min(d.budgets.max_steps for d in chain)
        timeout_ms = min(d.budgets.timeout_ms for d in chain)
        if self.cancel is not None and self.cancel.cancelled:
            raise _Stop(ExecReason.CANCELLED, "cancelled")
        if self.executor._clock.monotonic_ms() - self.start_ms >= timeout_ms:
            raise _Stop(ExecReason.TIMEOUT, f"exceeded timeout_ms={timeout_ms}")
        if self.result.steps_executed >= max_steps:
            raise _Stop(ExecReason.MAX_STEPS, f"exceeded max_steps={max_steps}")
        self.result.steps_executed += 1

    def _log(self, step: Step, ok: bool, tokens: int = 0, note: str = "") -> None:
        self.result.steps.append(StepLog(step.op, step.id, ok, tokens, note[:100]))

    # ---- expressions --------------------------------------------------------------------------
    def _var(self, dotted: str) -> Value:
        head, *rest = dotted.split(".")
        if head not in self.env:
            raise _Stop(ExecReason.UNBOUND, f"variable {head!r} is not bound")
        cur = self.env[head]
        for part in rest:
            if not isinstance(cur, dict) or part not in cur:
                raise _Stop(ExecReason.UNBOUND, f"{dotted!r} has no field {part!r}")
            cur = cur[part]
        return cur

    def _eval(self, expr: Expr) -> Value:
        if isinstance(expr, Literal_):
            return expr.literal
        if isinstance(expr, InputRef):
            if expr.input not in self.inputs:
                raise _Stop(ExecReason.UNBOUND, f"input {expr.input!r} not supplied")
            return self.inputs[expr.input]
        if isinstance(expr, VarRef):
            return self._var(expr.var)
        assert isinstance(expr, SelectPath)
        source = self._var(expr.select_path.from_var)
        paths = source if isinstance(source, list) else str(source).splitlines()
        wanted = expr.select_path.role
        matches = sorted({p for p in paths if isinstance(p, str) and classify_path(p) == wanted})
        if len(matches) != 1:
            raise _Stop(
                ExecReason.AMBIGUOUS_PATH,
                f"{len(matches)} files match role {wanted.value!r}; exactly one is required",
            )
        return matches[0]

    def _cond(self, cond: Cond) -> bool:
        if cond.operator == Operator.EXISTS:
            try:
                self._eval(cond.left)
            except _Stop:
                return False
            return True
        left = self._eval(cond.left)
        right = cond.right.literal if cond.right is not None else None
        if cond.operator == Operator.EQ:
            return bool(left == right)
        if cond.operator == Operator.NE:
            return bool(left != right)
        if cond.operator in (Operator.GT, Operator.LT):
            nums = (
                isinstance(left, int | float)
                and isinstance(right, int | float)
                and not isinstance(left, bool)
                and not isinstance(right, bool)
            )
            if not nums:
                return False
            return bool(left > right) if cond.operator == Operator.GT else bool(left < right)
        return isinstance(left, str) and isinstance(right, str) and right in left

    # ---- steps --------------------------------------------------------------------------------
    def exec_steps(self, steps: list[Step], depth: int) -> None:
        for step in steps:
            self._tick()
            if isinstance(step, ToolCall):
                self._tool(step)
            elif isinstance(step, LlmDecision):
                self._decision(step)
            elif isinstance(step, Assert):
                ok = self._cond(step.condition)
                self._log(step, ok)
                if not ok:
                    raise _Retryable(
                        "assert_failed", _Stop(ExecReason.ASSERT_FAILED, step.message_id)
                    )
            elif isinstance(step, Verify):
                self._verify_step(step)
            elif isinstance(step, Condition):
                self._log(step, True)
                self.exec_steps(
                    step.then if self._cond(step.condition) else step.otherwise, depth + 1
                )
            elif isinstance(step, Retry):
                self._retry(step, depth)
            elif isinstance(step, HumanApproval):
                self._log(step, False, note="human approval required")
                raise _Stop(ExecReason.HUMAN_APPROVAL_REQUIRED, step.reason_id, ExecStatus.ESCALATE)
            elif isinstance(step, Subcapability):
                self._subcapability(step)

    MAX_COMPOSE_DEPTH = 2

    def _subcapability(self, step: Subcapability) -> None:
        """Run a declared, ACTIVE dependency inside this run's budgets.

        The child shares the step/token/time counters (it can never exceed any ancestor's budget),
        gets a fresh variable scope seeded only with the arguments given, may only use tools its
        parent also permits, and must pass the same preconditions. Cycles and deep nesting stop.
        """
        resolve = self.executor._resolver
        child = resolve(step.capability) if resolve is not None else None
        if step.capability not in self.definition.dependencies or child is None:
            self._log(step, False, note="dependency not executable")
            raise _Stop(ExecReason.DEPENDENCY_UNAVAILABLE, f"{step.capability} is not executable")
        if len(self.stack) > self.MAX_COMPOSE_DEPTH:
            raise _Stop(ExecReason.COMPOSITION_LIMIT, "composition nested too deeply")
        if child.id in self.stack:
            raise _Stop(ExecReason.COMPOSITION_LIMIT, f"cycle through {child.id}")
        try:
            validate_definition(child, known_dependencies=child.dependencies)
        except CapabilityValidationError as exc:
            raise _Stop(ExecReason.INVALID_DEFINITION, str(exc)[:200]) from exc
        if not set(child.permissions) <= set(self.definition.permissions):
            self._log(step, False, note="child needs wider permissions")
            raise _Stop(
                ExecReason.TOOL_NOT_ALLOWED, "dependency needs tools its parent does not permit"
            )
        violations = check_preconditions(child, self.facts)
        if violations:
            raise _Stop(ExecReason.PRECONDITION_VIOLATED, "; ".join(violations)[:200])
        args = {name: self._eval(expr) for name, expr in step.args.items()}
        sub = _Run(
            executor=self.executor,
            definition=child,
            task=self.task,
            facts=self.facts,
            allowed=self.allowed & set(child.permissions),
            inputs=args,
            cancel=self.cancel,
            result=self.result,
            start_ms=self.start_ms,
            stack=(*self.stack, child.id),
            parent=self,
        )
        self._log(step, True, note=step.capability)
        try:
            sub.exec_steps(child.steps, depth=1)
        except _Retryable as retryable:  # a failure inside the child is the child's failure
            raise retryable.stop from retryable

    def _tool(self, step: ToolCall) -> None:
        contract = APPROVED_TOOLS.get(step.tool)
        if contract is None or step.tool not in self.allowed:
            self._log(step, False, note="tool not allowed")
            raise _Stop(ExecReason.TOOL_NOT_ALLOWED, f"tool {step.tool!r} is not allowed")
        args = {name: self._eval(expr) for name, expr in step.args.items()}
        if set(args) != contract.args:
            raise _Stop(ExecReason.INVALID_ARGS, f"bad arguments for {step.tool!r}")
        if self.result.tool_calls >= self.definition.budgets.max_steps:
            raise _Stop(ExecReason.MAX_STEPS, "tool call budget")
        try:
            out = self.executor._tools.call(step.tool, args)
        except ToolNotAllowedError as exc:
            self._log(step, False, note="gateway denied")
            raise _Stop(ExecReason.TOOL_NOT_ALLOWED, str(exc)[:200]) from exc
        self.result.tool_calls += 1
        self._log(step, out.ok, note=step.tool)
        if not out.ok:
            raise _Retryable("tool_failed", _Stop(ExecReason.TOOL_FAILED, out.output[:150]))
        if step.bind:
            self.env[step.bind] = (
                out.output.splitlines() if step.tool == "list_files" else out.output
            )

    def _decision(self, step: LlmDecision) -> None:
        max_tokens = min(d.budgets.max_tokens for d in self._chain())
        remaining = max_tokens - self.result.tokens_used
        if remaining <= 0:
            raise _Stop(ExecReason.MAX_TOKENS, f"exceeded max_tokens={max_tokens}")
        observed: dict[str, str] = {}
        for name in step.inputs:
            value = self._var(name)
            observed[name] = "\n".join(value) if isinstance(value, list) else str(value)
        try:
            decision = self.executor._decisions.decide(
                template_id=step.template_id,
                objective=self.task.objective,
                inputs=observed,
                output_schema=step.output_schema,
                max_tokens=min(step.max_tokens, remaining),
            )
        except DecisionError as exc:
            self._log(step, False, note="decision failed")
            raise _Stop(ExecReason.DECISION_FAILED, str(exc)[:200]) from exc
        self.result.tokens_used += decision.tokens
        self._log(step, True, decision.tokens)
        if self.result.tokens_used > max_tokens:
            raise _Stop(ExecReason.MAX_TOKENS, f"exceeded max_tokens={max_tokens}")
        if set(decision.values) != set(step.output_schema):
            raise _Stop(ExecReason.DECISION_FAILED, "decision output keys do not match the schema")
        for key, kind in step.output_schema.items():
            v = decision.values[key]
            ok = (
                (kind == OutType.STRING and isinstance(v, str))
                or (
                    kind == OutType.NUMBER
                    and isinstance(v, int | float)
                    and not isinstance(v, bool)
                )
                or (kind == OutType.BOOLEAN and isinstance(v, bool))
            )
            if not ok:
                raise _Stop(
                    ExecReason.DECISION_FAILED, f"decision field {key!r} has the wrong type"
                )
        self.env[step.bind] = dict(decision.values)

    def _verify_step(self, step: Verify) -> None:
        outcome = self.executor._verify()
        self._log(step, outcome is True, note=str(outcome))
        if outcome is True:
            self.result.verified_by_capability = True
            return
        reason = ExecReason.VERIFY_FAILED if outcome is False else ExecReason.VERIFY_UNKNOWN
        raise _Stop(reason, "independent verification did not pass")

    def _retry(self, step: Retry, depth: int) -> None:
        self._log(step, True)
        last: _Stop | None = None
        for _attempt in range(step.max_attempts):
            try:
                self.exec_steps(step.steps, depth + 1)
                return
            except _Retryable as r:
                if r.kind != step.retry_on:
                    raise _Stop(r.stop.reason, r.stop.detail) from r
                last = r.stop
        raise _Stop(ExecReason.RETRY_EXHAUSTED, (last.detail if last else "")[:200])
