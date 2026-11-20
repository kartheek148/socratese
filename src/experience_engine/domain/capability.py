"""Capability schema and restricted DSL (milestone 3).

A capability is *data*: an immutable, versioned, declarative procedure over an allow-listed set of
primitives. Nothing here can carry executable code: every field is typed, unknown fields are
rejected, and expressions are structured references rather than strings to be evaluated. The
semantic validator lives in `capabilities/validator.py`; the executor arrives in milestone 4.
"""

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

JsonScalar = str | int | float | bool | None
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- lifecycle -------------------------------------------------------------------------------
class CapabilityState(StrEnum):
    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    DEPRECATED = "DEPRECATED"
    ROLLED_BACK = "ROLLED_BACK"


ALLOWED_TRANSITIONS: dict[CapabilityState, frozenset[CapabilityState]] = {
    CapabilityState.DISCOVERED: frozenset({CapabilityState.CANDIDATE, CapabilityState.REJECTED}),
    CapabilityState.CANDIDATE: frozenset({CapabilityState.VALIDATING, CapabilityState.REJECTED}),
    CapabilityState.VALIDATING: frozenset({CapabilityState.APPROVED, CapabilityState.REJECTED}),
    CapabilityState.APPROVED: frozenset({CapabilityState.SHADOW, CapabilityState.REJECTED}),
    CapabilityState.SHADOW: frozenset({CapabilityState.CANARY, CapabilityState.ROLLED_BACK}),
    CapabilityState.CANARY: frozenset({CapabilityState.ACTIVE, CapabilityState.ROLLED_BACK}),
    CapabilityState.ACTIVE: frozenset({CapabilityState.DEPRECATED, CapabilityState.ROLLED_BACK}),
    CapabilityState.REJECTED: frozenset(),
    # DEPRECATED -> ACTIVE is the restore step of a rollback to the previously active version; the
    # registry only performs it as part of `rollback`, never as a standalone promotion.
    CapabilityState.DEPRECATED: frozenset({CapabilityState.ACTIVE}),
    CapabilityState.ROLLED_BACK: frozenset(),
}

# Only these states may be executed by normal task execution. Candidates never are.
EXECUTABLE_STATES = frozenset({CapabilityState.ACTIVE})


def can_transition(src: CapabilityState, dst: CapabilityState) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


# ---- expressions (structured references, never evaluated strings) ------------------------------
class PathRole(StrEnum):
    """Deterministic classification of a file path (see capabilities/tools.py)."""

    LOG = "log"
    INFRA = "infra"
    CONFIG = "config"


class Literal_(_Frozen):
    literal: JsonScalar


class InputRef(_Frozen):
    input: Identifier


class VarRef(_Frozen):
    var: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}(\.[a-z][a-z0-9_]{0,63}){0,3}$")]


class SelectPathSpec(_Frozen):
    from_var: Identifier
    role: PathRole


class SelectPath(_Frozen):
    select_path: SelectPathSpec


Expr = Literal_ | InputRef | VarRef | SelectPath


class Operator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    LT = "lt"
    IN = "in"
    EXISTS = "exists"


class Cond(_Frozen):
    left: Expr
    operator: Operator
    right: Literal_ | None = None


class OutType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


# ---- steps: the only allowed primitives --------------------------------------------------------
class ToolCall(_Frozen):
    op: Literal["TOOL_CALL"] = "TOOL_CALL"
    id: Identifier
    tool: Identifier
    args: dict[Identifier, Expr] = Field(default_factory=dict)
    bind: Identifier | None = None


class LlmDecision(_Frozen):
    """Bounded model call. Uses an approved template id (never free-form prompt text) and a
    flat typed output schema; the output can only be consumed as data by later steps.
    """

    op: Literal["LLM_DECISION"] = "LLM_DECISION"
    id: Identifier
    template_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}/[0-9]{1,3}$")]
    inputs: list[Identifier] = Field(default_factory=list, max_length=16)
    output_schema: dict[Identifier, OutType] = Field(min_length=1, max_length=16)
    bind: Identifier
    max_tokens: int = Field(gt=0, le=8000)


class Assert(_Frozen):
    op: Literal["ASSERT"] = "ASSERT"
    id: Identifier
    condition: Cond
    message_id: Identifier


class Subcapability(_Frozen):
    op: Literal["SUBCAPABILITY"] = "SUBCAPABILITY"
    id: Identifier
    capability: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}@[0-9]{1,6}$")]
    args: dict[Identifier, Expr] = Field(default_factory=dict)


class Verify(_Frozen):
    op: Literal["VERIFY"] = "VERIFY"
    id: Identifier
    verifier: Identifier


class HumanApproval(_Frozen):
    op: Literal["HUMAN_APPROVAL"] = "HUMAN_APPROVAL"
    id: Identifier
    reason_id: Identifier


class Condition(_Frozen):
    op: Literal["CONDITION"] = "CONDITION"
    id: Identifier
    condition: Cond
    then: list["Step"] = Field(default_factory=list)
    otherwise: list["Step"] = Field(default_factory=list)


class Retry(_Frozen):
    op: Literal["RETRY"] = "RETRY"
    id: Identifier
    max_attempts: int = Field(ge=1, le=5)
    retry_on: Literal["tool_failed", "assert_failed"]
    steps: list["Step"] = Field(min_length=1)


Step = Annotated[
    ToolCall | LlmDecision | Assert | Subcapability | Verify | HumanApproval | Condition | Retry,
    Field(discriminator="op"),
]
Condition.model_rebuild()
Retry.model_rebuild()


# ---- preconditions and boundaries --------------------------------------------------------------
class TaskFamilyIs(_Frozen):
    kind: Literal["task_family_is"] = "task_family_is"
    family: Identifier


class ToolsAvailable(_Frozen):
    kind: Literal["tools_available"] = "tools_available"
    tools: list[Identifier] = Field(min_length=1)


class FileRolePresent(_Frozen):
    kind: Literal["file_role_present"] = "file_role_present"
    role: PathRole


class RiskLevelAtMost(_Frozen):
    kind: Literal["risk_level_at_most"] = "risk_level_at_most"
    level: Literal["low", "medium", "high"]


Precondition = Annotated[
    TaskFamilyIs | ToolsAvailable | FileRolePresent | RiskLevelAtMost, Field(discriminator="kind")
]


class TerminationRule(_Frozen):
    when: Literal["verify_passed", "budget_exhausted", "precondition_violated", "step_failed"]
    outcome: Literal["SUCCESS", "FALLBACK", "ESCALATE"]


class FailureCondition(_Frozen):
    kind: Literal["verification_failed", "tool_error", "budget_exceeded", "uncertain_observation"]
    description_id: Identifier


class RecoveryAction(_Frozen):
    on: Literal["verification_failed", "tool_error", "budget_exceeded", "uncertain_observation"]
    action: Literal["FALLBACK_GENERAL_PLANNING", "HUMAN_ESCALATION"]


class Budgets(_Frozen):
    max_steps: int = Field(gt=0, le=100)
    max_tokens: int = Field(gt=0, le=50_000)
    timeout_ms: int = Field(gt=0, le=600_000)


class Provenance(_Frozen):
    """Where a definition came from. Contains no timestamps so the content hash is stable."""

    source_run_ids: list[str] = Field(min_length=1)
    counterexample_run_ids: list[str] = Field(default_factory=list)
    compiler_version: str = Field(min_length=1)
    prompt_version: str | None = None
    model_version: str | None = None
    pipeline_version: str = Field(min_length=1)
    dataset_name: str | None = None
    dataset_version: str | None = None


class FieldType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_LIST = "string_list"


class CapabilityDefinition(_Frozen):
    """Immutable once registered as a version."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,40}\.[0-9a-f]{10}$")]
    version: int = Field(ge=1)
    objective: str = Field(min_length=1, max_length=300)
    inputs_schema: dict[Identifier, FieldType] = Field(default_factory=dict)
    outputs_schema: dict[Identifier, FieldType] = Field(default_factory=dict)
    preconditions: list[Precondition]
    steps: list[Step] = Field(min_length=1)
    termination: list[TerminationRule] = Field(min_length=1)
    failure_conditions: list[FailureCondition] = Field(default_factory=list)
    recovery: list[RecoveryAction] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    permissions: list[Identifier] = Field(default_factory=list)
    budgets: Budgets
    known_limitations: list[str] = Field(default_factory=list, max_length=20)
    provenance: Provenance


class CandidateTest(_Frozen):
    """Data-only test case, executed by the milestone-4 evaluator (never by the compiler)."""

    kind: Literal["SOURCE_REPLAY", "NEGATIVE_APPLICABILITY", "COUNTEREXAMPLE"]
    task_id: str | None = None
    run_id: str | None = None
    expected: Literal["VERIFIED_SUCCESS", "NOT_APPLICABLE", "FAILURE"]
    note: str = Field(default="", max_length=300)


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def definition_hash(definition: CapabilityDefinition) -> str:
    """Integrity hash of the full immutable definition (includes version and provenance)."""
    return hashlib.sha256(_canonical(definition.model_dump(mode="json")).encode()).hexdigest()


def procedure_hash(definition: CapabilityDefinition) -> str:
    """Identity of the *procedure*: ignores version, the lists of source runs and descriptive
    limitations, so new evidence for the same procedure does not look like a new version.
    """
    data = definition.model_dump(mode="json")
    data.pop("version")
    data.pop("known_limitations")
    data["provenance"].pop("source_run_ids")
    data["provenance"].pop("counterexample_run_ids")
    return hashlib.sha256(_canonical(data).encode()).hexdigest()


class CandidateBundle(_Frozen):
    definition: CapabilityDefinition
    tests: list[CandidateTest]
    procedure_hash: str
    skeleton_signature: str
    task_family: str


class RegistrationKind(StrEnum):
    NEW_CAPABILITY = "NEW_CAPABILITY"
    NEW_VERSION = "NEW_VERSION"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    NO_CHANGE = "NO_CHANGE"
