"""Hand-authored capability fixtures for controller stress experiments (labelled as such).

`good` mirrors what the compiler emits for these families (read config, logs and infra, ask the
bounded decision template, apply, verify). `drifted` is the same procedure with the target key
frozen to a layout that no longer exists, i.e. a capability that has become stale after the
environment's config layout changed (it was redesigned once after a development run showed that a
key frozen to a layout that still exists in some variants only produced a variant-dependent mix;
see docs/experiments/adaptive-controller-design.md §12). Both are validated through the normal
DSL validator; their real quality is *measured*, never assumed, and they are never promoted or
registered.
"""

from experience_engine.capabilities.validator import validate_definition
from experience_engine.domain.capability import (
    Budgets,
    CapabilityDefinition,
    FailureCondition,
    FieldType,
    Literal_,
    LlmDecision,
    OutType,
    PathRole,
    Provenance,
    RecoveryAction,
    RiskLevelAtMost,
    SelectPath,
    SelectPathSpec,
    Step,
    TaskFamilyIs,
    TerminationRule,
    ToolCall,
    ToolsAvailable,
    VarRef,
    Verify,
)

TOOLS = ["list_files", "read_file", "set_json_value"]
# a key from a retired config layout: absent from every current variant, so the tool reports
# "key not found", the capability hands over to the planner and has only spent its own tokens
RETIRED_KEY = {"pool": "database.legacy_pool_size", "timeout": "http.legacy_timeout_ms"}
VALUE_TYPE = {"pool": OutType.NUMBER, "timeout": OutType.NUMBER}


def _select(role: PathRole) -> SelectPath:
    return SelectPath(select_path=SelectPathSpec(from_var="files", role=role))


def build(family: str, *, drifted: bool, tag: str) -> CapabilityDefinition:
    reads = [PathRole.CONFIG, PathRole.LOG, PathRole.INFRA]
    steps: list[Step] = [ToolCall(id="list_files", tool="list_files", bind="files")]
    for role in reads:
        steps.append(
            ToolCall(
                id=f"read_{role.value}",
                tool="read_file",
                args={"path": _select(role)},
                bind=f"{role.value}_text",
            )
        )
    key_expr = Literal_(literal=RETIRED_KEY[family]) if drifted else VarRef(var="fix.key")
    steps += [
        LlmDecision(
            id="decide_fix",
            template_id="select_fix_parameters/1",
            inputs=[f"{r.value}_text" for r in reads],
            output_schema={"key": OutType.STRING, "value": VALUE_TYPE[family]},
            bind="fix",
            max_tokens=2000,
        ),
        ToolCall(
            id="apply_fix",
            tool="set_json_value",
            args={
                "path": _select(PathRole.CONFIG),
                "key": key_expr,
                "value": VarRef(var="fix.value"),
            },
            bind="applied",
        ),
        Verify(id="verify_outcome", verifier="outcome_verifier"),
    ]
    definition = CapabilityDefinition(
        id=f"{family}.{tag}",
        version=1,
        objective=f"Resolve {family} problems using the stress-fixture procedure.",
        inputs_schema={},
        outputs_schema={"verified": FieldType.BOOLEAN},
        preconditions=[
            TaskFamilyIs(family=family),
            ToolsAvailable(tools=TOOLS),
            RiskLevelAtMost(level="low"),
        ],
        steps=steps,
        termination=[
            TerminationRule(when="verify_passed", outcome="SUCCESS"),
            TerminationRule(when="budget_exhausted", outcome="FALLBACK"),
            TerminationRule(when="step_failed", outcome="FALLBACK"),
        ],
        failure_conditions=[
            FailureCondition(kind="verification_failed", description_id="outcome_not_verified"),
            FailureCondition(kind="tool_error", description_id="tool_returned_error"),
        ],
        recovery=[
            RecoveryAction(on="verification_failed", action="FALLBACK_GENERAL_PLANNING"),
            RecoveryAction(on="tool_error", action="FALLBACK_GENERAL_PLANNING"),
        ],
        permissions=TOOLS,
        budgets=Budgets(max_steps=12, max_tokens=8000, timeout_ms=60_000),
        known_limitations=[
            "STRESS FIXTURE (hand-authored); "
            + ("target key frozen to a retired layout" if drifted else "compiler-shaped procedure")
        ],
        provenance=Provenance(
            source_run_ids=["stress-fixture"],  # hand-authored: no source run exists
            compiler_version="stress-fixture/1",
            pipeline_version="stress-fixture/1",
        ),
    )
    return validate_definition(definition)


def portfolio(name: str) -> list[CapabilityDefinition]:
    """`good`: compiler-shaped capabilities for pool and timeout. `heterogeneous`: a good pool
    capability and a drifted timeout capability. `drifted`: both drifted."""
    if name == "good":
        return [
            build("pool", drifted=False, tag="f00d000001"),
            build("timeout", drifted=False, tag="f00d000002"),
        ]
    if name == "heterogeneous":
        return [
            build("pool", drifted=False, tag="f00d000001"),
            build("timeout", drifted=True, tag="5ca1e00002"),
        ]
    if name == "drifted":
        return [
            build("pool", drifted=True, tag="5ca1e00001"),
            build("timeout", drifted=True, tag="5ca1e00002"),
        ]
    raise ValueError(f"unknown portfolio {name!r} (good | heterogeneous | drifted)")
