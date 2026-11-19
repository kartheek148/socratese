from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from experience_engine.domain.trace import MetricValue, TraceEventType


@dataclass(frozen=True)
class Payload:
    text: str
    content_type: str = "text/plain"


@dataclass(frozen=True)
class EmitSpec:
    type: TraceEventType
    component: str
    component_version: str
    action: str
    status: str
    metrics: Mapping[str, MetricValue] = field(default_factory=dict)
    payloads: Mapping[str, Payload] = field(default_factory=dict)
    parent_event_id: str | None = None


class TraceSink(Protocol):
    def emit(self, spec: EmitSpec) -> str:
        """Record one event and return its event_id. Must not raise for payload problems."""
        ...
