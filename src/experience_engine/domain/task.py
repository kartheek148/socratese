from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

JsonScalar = str | int | float | bool | None


class _Criterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PoolCapacityCriterion(_Criterion):
    """Sum of the capacity keys (dotted JSON paths) must lie in [min_capacity, max_capacity]."""

    kind: Literal["pool_capacity"] = "pool_capacity"
    role: Literal["goal", "guard"] = "goal"
    config_path: str
    capacity_keys: list[str] = Field(min_length=1)
    min_capacity: int
    max_capacity: int


class TimeoutCoversLatencyCriterion(_Criterion):
    """Configured timeout (value * unit_ms) must cover every observed latency and stay under cap."""

    kind: Literal["timeout_covers_latency"] = "timeout_covers_latency"
    role: Literal["goal", "guard"] = "goal"
    config_path: str
    timeout_key: str
    unit_ms: int = Field(gt=0)
    latency_samples_ms: list[int] = Field(min_length=1)
    max_timeout_ms: int


class ValueUnchangedCriterion(_Criterion):
    """Guard: a value that must not be modified by the agent."""

    kind: Literal["value_unchanged"] = "value_unchanged"
    role: Literal["goal", "guard"] = "guard"
    path: str
    key: str
    expected: JsonScalar


SuccessCriterion = Annotated[
    PoolCapacityCriterion | TimeoutCoversLatencyCriterion | ValueUnchangedCriterion,
    Field(discriminator="kind"),
]


class TaskSpec(BaseModel):
    """Task contract. Success criteria are caller-supplied and never shown to the agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    tenant_id: str
    objective: str
    inputs: dict[str, object] = Field(default_factory=dict)
    context_refs: list[str] = Field(default_factory=list)
    task_family: str | None = None
    success_criteria: list[SuccessCriterion] = Field(min_length=1)
    max_tokens: int = Field(gt=0)
    max_steps: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    deadline_ms: int = Field(gt=0)
    risk_level: Literal["low", "medium", "high"]
    allowed_tools: list[str] = Field(default_factory=list)
