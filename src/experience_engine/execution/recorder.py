import json

from experience_engine.domain.ids import new_id
from experience_engine.domain.redaction import Redactor
from experience_engine.domain.trace import ArtifactRef, TraceEvent
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.persistence import RunLifecycle
from experience_engine.ports.tracing import EmitSpec

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_TRUNCATION_MARKER = "\n[truncated]"


class NullTraceSink:
    """Used when persistence is not configured (e.g. the DB-free benchmark path)."""

    def emit(self, spec: EmitSpec) -> str:
        return new_id()


class RunRecorder:
    """Turns emit() calls into append-only TraceEvents.

    Redacts every string before persistence, moves payloads into the artifact store and keeps
    only references in the event row. Sequence numbers are assigned here, per run.
    """

    def __init__(
        self,
        *,
        lifecycle: RunLifecycle,
        artifacts: ArtifactStore,
        redactor: Redactor,
        clock: Clock,
        tenant_id: str,
        task_id: str,
        run_id: str,
        trace_id: str,
    ):
        self._lifecycle = lifecycle
        self._artifacts = artifacts
        self._redactor = redactor
        self._clock = clock
        self._tenant_id = tenant_id
        self._task_id = task_id
        self._run_id = run_id
        self._trace_id = trace_id
        self._sequence = 0

    def emit(self, spec: EmitSpec) -> str:
        refs: list[ArtifactRef] = []
        for role, payload in spec.payloads.items():
            result = self._redactor.redact(payload.text)
            data = result.text.encode()
            truncated = len(data) > MAX_ARTIFACT_BYTES
            if truncated:
                data = data[:MAX_ARTIFACT_BYTES] + _TRUNCATION_MARKER.encode()
            stored = self._artifacts.put(
                self._tenant_id,
                data,
                role=role,
                content_type=payload.content_type,
                redacted=result.redacted,
                truncated=truncated,
            )
            self._lifecycle.register_artifact(self._tenant_id, stored.ref, stored.uri)
            refs.append(stored.ref)

        metrics = {
            self._redactor.redact_text(k)[:64]: (
                self._redactor.redact_text(v)[:256] if isinstance(v, str) else v
            )
            for k, v in spec.metrics.items()
        }
        event = TraceEvent(
            event_id=new_id(),
            tenant_id=self._tenant_id,
            task_id=self._task_id,
            run_id=self._run_id,
            trace_id=self._trace_id,
            sequence=self._sequence,
            parent_event_id=spec.parent_event_id,
            type=spec.type,
            component=spec.component,
            component_version=spec.component_version,
            action=self._redactor.redact_text(spec.action)[:200],
            status=spec.status,
            metrics=metrics,
            artifact_refs=refs,
            ts=self._clock.now(),
        )
        self._lifecycle.append_event(event)
        self._sequence += 1
        return event.event_id


def json_payload(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
