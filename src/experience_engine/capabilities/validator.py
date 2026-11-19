"""Static validation of capability definitions.

`validate_definition(raw)` is the only way a definition enters the system from untrusted input
(compiler output, LLM-proposed JSON, imports). It never executes anything. A definition is
rejected when it:
  * fails the typed schema (unknown fields/ops, bad identifiers, out-of-range budgets);
  * has no preconditions, or no task-family precondition (applicability must be explicit);
  * uses an unapproved tool, wrong tool arguments, or an unapproved template/verifier;
  * references variables/inputs that are never bound (data-flow check);
  * contains code-, shell-, SQL- or import-like strings in any string value;
  * exceeds structural limits (steps, depth), or its permissions do not cover the tools used.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import ValidationError

from experience_engine.capabilities.tools import (
    APPROVED_TEMPLATES,
    APPROVED_TOOLS,
    APPROVED_VERIFIERS,
)
from experience_engine.domain.capability import (
    Assert,
    CapabilityDefinition,
    Cond,
    Condition,
    Expr,
    InputRef,
    LlmDecision,
    Retry,
    SelectPath,
    Step,
    Subcapability,
    TaskFamilyIs,
    ToolCall,
    ToolsAvailable,
    VarRef,
    Verify,
)

MAX_TOTAL_STEPS = 50
MAX_DEPTH = 3

_DANGEROUS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"__\w+__",
        r"\b(eval|exec|compile|getattr|setattr|globals|locals|open)\s*\(",
        r"\bimport\s+\w",
        r"\bfrom\s+\w+\s+import\b",
        r"\blambda\b",
        r"\b(subprocess|os\.system|popen|sudo|chmod|curl|wget|bash|sh\s+-c)\b",
        r"(;|&&|\|\|)\s*(rm|cat|curl|wget|sh|bash)\b",
        r"`|\$\(|\$\{",
        r"\b(drop|delete|truncate|alter|insert|update)\s+(table|from|into)\b",
        r"\bunion\s+select\b",
        r"<script",
    )
]


class CapabilityValidationError(Exception):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues[:5]) + (" ..." if len(issues) > 5 else ""))
        self.issues = issues


@dataclass
class _Ctx:
    issues: list[str]
    bound: set[str]
    inputs: set[str]
    total_steps: int = 0
    tools_used: set[str] = field(default_factory=set)

    def err(self, msg: str) -> None:
        self.issues.append(msg)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _check_expr(expr: Expr, ctx: _Ctx, where: str) -> None:
    if isinstance(expr, InputRef) and expr.input not in ctx.inputs:
        ctx.err(f"{where}: input '{expr.input}' is not declared in inputs_schema")
    elif isinstance(expr, VarRef):
        if expr.var.split(".")[0] not in ctx.bound:
            ctx.err(f"{where}: variable '{expr.var}' used before it is bound")
    elif isinstance(expr, SelectPath) and expr.select_path.from_var not in ctx.bound:
        ctx.err(f"{where}: variable '{expr.select_path.from_var}' used before it is bound")


def _check_cond(cond: Cond, ctx: _Ctx, where: str) -> None:
    _check_expr(cond.left, ctx, where)
    if cond.operator.value != "exists" and cond.right is None:
        ctx.err(f"{where}: operator '{cond.operator.value}' needs a right-hand literal")


def _check_steps(steps: list[Step], ctx: _Ctx, depth: int, known_deps: set[str]) -> None:
    if depth > MAX_DEPTH:
        ctx.err(f"steps nested deeper than {MAX_DEPTH}")
        return
    seen: set[str] = set()
    for step in steps:
        ctx.total_steps += 1
        where = f"step '{step.id}'"
        if step.id in seen:
            ctx.err(f"{where}: duplicate step id")
        seen.add(step.id)
        if isinstance(step, ToolCall):
            contract = APPROVED_TOOLS.get(step.tool)
            if contract is None:
                ctx.err(f"{where}: tool '{step.tool}' is not approved for capabilities")
            elif set(step.args) != contract.args:
                ctx.err(
                    f"{where}: tool '{step.tool}' requires args {sorted(contract.args)}, "
                    f"got {sorted(step.args)}"
                )
            for name, expr in step.args.items():
                _check_expr(expr, ctx, f"{where} arg '{name}'")
            if step.bind:
                ctx.bound.add(step.bind)
        elif isinstance(step, LlmDecision):
            if step.template_id not in APPROVED_TEMPLATES:
                ctx.err(f"{where}: template '{step.template_id}' is not approved")
            for name in step.inputs:
                if name not in ctx.bound:
                    ctx.err(f"{where}: input variable '{name}' is not bound")
            ctx.bound.add(step.bind)
        elif isinstance(step, Assert):
            _check_cond(step.condition, ctx, where)
        elif isinstance(step, Subcapability):
            if step.capability not in known_deps:
                ctx.err(f"{where}: dependency '{step.capability}' is not declared")
            for name, expr in step.args.items():
                _check_expr(expr, ctx, f"{where} arg '{name}'")
        elif isinstance(step, Verify):
            if step.verifier not in APPROVED_VERIFIERS:
                ctx.err(f"{where}: verifier '{step.verifier}' is not approved")
        elif isinstance(step, Condition):
            _check_cond(step.condition, ctx, where)
            before = set(ctx.bound)
            _check_steps(step.then, ctx, depth + 1, known_deps)
            then_bound, ctx.bound = ctx.bound, set(before)
            _check_steps(step.otherwise, ctx, depth + 1, known_deps)
            # only bindings made on both branches are visible afterwards
            ctx.bound = before | (then_bound & ctx.bound)
        elif isinstance(step, Retry):
            _check_steps(step.steps, ctx, depth + 1, known_deps)


def tools_used(steps: list[Step]) -> set[str]:
    used: set[str] = set()
    for step in steps:
        if isinstance(step, ToolCall):
            used.add(step.tool)
        elif isinstance(step, Condition):
            used |= tools_used(step.then) | tools_used(step.otherwise)
        elif isinstance(step, Retry):
            used |= tools_used(step.steps)
    return used


def validate_definition(
    raw: Mapping[str, object] | CapabilityDefinition,
    known_dependencies: Iterable[str] = (),
) -> CapabilityDefinition:
    """Validate untrusted input and return the typed definition, or raise
    CapabilityValidationError listing every problem found."""
    issues: list[str] = []
    payload = raw.model_dump(mode="json") if isinstance(raw, CapabilityDefinition) else raw
    for text in _strings(payload):
        for pattern in _DANGEROUS:
            if pattern.search(text):
                issues.append(f"unsafe content in a string value (matches {pattern.pattern!r})")
                break
    try:
        definition = CapabilityDefinition.model_validate(payload)
    except ValidationError as exc:
        schema_issues = [
            f"schema: {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        ]
        raise CapabilityValidationError(issues + schema_issues) from exc

    preconds = definition.preconditions
    if not any(isinstance(p, TaskFamilyIs) for p in preconds):
        issues.append("preconditions must include task_family_is (explicit applicability boundary)")
    if not any(isinstance(p, ToolsAvailable) for p in preconds):
        issues.append("preconditions must include tools_available")

    deps = set(definition.dependencies)
    if definition.id in {d.split("@")[0] for d in deps}:
        issues.append("a capability cannot depend on itself")
    unknown_deps = deps - set(known_dependencies)
    if unknown_deps:
        issues.append(f"unknown dependencies: {sorted(unknown_deps)}")

    ctx = _Ctx(issues=issues, bound=set(), inputs=set(definition.inputs_schema))
    _check_steps(definition.steps, ctx, 1, deps)
    if ctx.total_steps > MAX_TOTAL_STEPS:
        issues.append(f"more than {MAX_TOTAL_STEPS} steps")
    if ctx.total_steps > definition.budgets.max_steps:
        issues.append("budgets.max_steps is smaller than the number of steps")

    used = tools_used(definition.steps)
    missing = used - set(definition.permissions)
    if missing:
        issues.append(f"permissions do not cover tools used: {sorted(missing)}")
    needed = {t for p in preconds if isinstance(p, ToolsAvailable) for t in p.tools}
    if used - needed:
        issues.append(f"tools_available does not list tools used: {sorted(used - needed)}")
    if issues:
        raise CapabilityValidationError(issues)
    return definition
