"""Wiring for capability-assisted execution: tool-call tracing and the runtime bundle."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from experience_engine import RUNTIME_VERSION
from experience_engine.domain.capability import CapabilityDefinition
from experience_engine.domain.trace import TraceEventType
from experience_engine.execution.matcher import CapabilityMatcher
from experience_engine.ports.decisions import DecisionProvider
from experience_engine.ports.experience import ExecutableCapabilityCatalog
from experience_engine.ports.llm import LLMClient
from experience_engine.ports.tools import ToolGateway, ToolNotAllowedError, ToolResult, ToolSpec
from experience_engine.ports.tracing import EmitSpec, Payload, TraceSink

_JSON = "application/json"


@dataclass(frozen=True)
class CapabilityRuntime:
    """Everything the orchestrator needs to offer capabilities to a run.

    `decisions_for` binds a DecisionProvider to the run's LLM client; the catalog is the
    read-only, ACTIVE-only view (or an explicitly experimental one, which tags runs).
    """

    catalog: ExecutableCapabilityCatalog
    matcher: CapabilityMatcher
    decisions_for: Callable[[LLMClient], DecisionProvider]
    experimental: bool = False


class StaticCapabilityCatalog:
    """In-memory, tenant-scoped catalog over an explicit set of definitions.

    For offline experiments only: the caller decides which versions are visible (so it must
    label results as experimental unless every version is ACTIVE). Never used by production
    wiring, which reads the registry through `SqlExecutableCatalog`.
    """

    def __init__(self, tenant_id: str, definitions: Sequence[CapabilityDefinition]):
        self._tenant = tenant_id
        self._defs = {(d.id, d.version): d for d in definitions}

    def list_executable(self, tenant_id: str) -> list[CapabilityDefinition]:
        if tenant_id != self._tenant:
            return []
        return [self._defs[k] for k in sorted(self._defs)]

    def get_executable(
        self, tenant_id: str, capability_id: str, version: int
    ) -> CapabilityDefinition | None:
        return self._defs.get((capability_id, version)) if tenant_id == self._tenant else None


class TracingToolGateway:
    """Records capability tool calls as TOOL_CALL events, like the general-planning runtime does."""

    def __init__(self, inner: ToolGateway, sink: TraceSink, parent_event_id: str | None):
        self._inner = inner
        self._sink = sink
        self._last = parent_event_id

    def describe(self) -> list[ToolSpec]:
        return self._inner.describe()

    def call(self, tool: str, args: Mapping[str, object]) -> ToolResult:
        args_payload = Payload(json.dumps(dict(args), sort_keys=True, default=str), _JSON)
        try:
            result = self._inner.call(tool, args)
        except ToolNotAllowedError as exc:
            self._last = self._sink.emit(
                EmitSpec(
                    type=TraceEventType.TOOL_CALL,
                    component="capability_tool_gateway",
                    component_version=RUNTIME_VERSION,
                    action=tool[:100],
                    status="denied",
                    metrics={"reason": str(exc)[:200]},
                    payloads={"args": args_payload},
                    parent_event_id=self._last,
                )
            )
            raise
        self._last = self._sink.emit(
            EmitSpec(
                type=TraceEventType.TOOL_CALL,
                component="capability_tool_gateway",
                component_version=RUNTIME_VERSION,
                action=tool[:100],
                status="ok" if result.ok else "failed",
                metrics={"output_bytes": len(result.output.encode())},
                payloads={"args": args_payload, "output": Payload(result.output)},
                parent_event_id=self._last,
            )
        )
        return result

    @property
    def last_event_id(self) -> str | None:
        return self._last
