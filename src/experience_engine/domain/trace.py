from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from experience_engine.domain.time import UtcDatetime

MetricValue = str | int | float | bool | None
MAX_METRIC_KEYS = 32
MAX_METRIC_STR = 256


class TraceEventType(StrEnum):
    DECISION = "DECISION"
    MODEL_CALL = "MODEL_CALL"
    TOOL_CALL = "TOOL_CALL"
    INVALID_ACTION = "INVALID_ACTION"
    RUN_TERMINATED = "RUN_TERMINATED"
    VERIFICATION = "VERIFICATION"


class ArtifactRef(BaseModel):
    """Pointer to a large payload held in the artifact store (tenant-scoped, content-addressed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    content_type: str = Field(max_length=100)
    redacted: bool = False
    truncated: bool = False


class TraceEvent(BaseModel):
    """Immutable, append-only execution event. Carries bounded metadata and artifact references,
    never raw payloads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    tenant_id: str
    task_id: str
    run_id: str
    trace_id: str
    sequence: int = Field(ge=0)
    parent_event_id: str | None = None
    type: TraceEventType
    component: str = Field(max_length=64)
    component_version: str = Field(max_length=100)
    action: str = Field(max_length=200)
    status: str = Field(max_length=32)
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    ts: UtcDatetime

    @field_validator("metrics")
    @classmethod
    def _bounded_metrics(cls, value: dict[str, MetricValue]) -> dict[str, MetricValue]:
        if len(value) > MAX_METRIC_KEYS:
            raise ValueError(f"at most {MAX_METRIC_KEYS} metrics per event")
        for k, v in value.items():
            if len(k) > 64 or (isinstance(v, str) and len(v) > MAX_METRIC_STR):
                raise ValueError("metric key/value too large; store payloads as artifacts")
        return value


class StepRecord(BaseModel):
    """Bounded per-step summary used by benchmark run records. Raw payloads are never stored;
    only size and content hash. Full evidence lives in TraceEvent + artifacts (milestone 2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    kind: Literal["llm", "tool"]
    tool: str | None = None
    ok: bool | None = None
    tokens: int = 0
    summary: str = Field(max_length=200)
    size_bytes: int = 0
    sha256: str | None = None
