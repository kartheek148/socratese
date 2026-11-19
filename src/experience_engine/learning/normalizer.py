"""Rebuild a normalized trajectory from a persisted run (trace events + artifacts)."""

import contextlib
import hashlib
import json
from collections.abc import Callable

from experience_engine.capabilities.tools import classify_path
from experience_engine.domain.capability import PathRole
from experience_engine.domain.experience import NormalizedStep, Trajectory
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.trace import TraceEvent, TraceEventType
from experience_engine.ports.persistence import RunReconstruction

ArtifactReader = Callable[[str, str], bytes]  # (tenant_id, sha256) -> content


class IncompleteTraceError(Exception):
    """The run has no outcome or its trace lacks required events; it cannot be learned from."""


def _artifact(events: TraceEvent, role: str) -> str | None:
    return next((r.sha256 for r in events.artifact_refs if r.role == role), None)


def _load_json(read: ArtifactReader, tenant: str, event: TraceEvent, role: str) -> object:
    sha = _artifact(event, role)
    if sha is None:
        return None
    try:
        return json.loads(read(tenant, sha))
    except (ValueError, KeyError):
        return None


def _value_type(value: object) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _shape(doc: object) -> str | None:
    """Variant fingerprint: hash of the config file's top-level key names (not values)."""
    if not isinstance(doc, dict):
        return None
    return hashlib.sha256(",".join(sorted(doc)).encode()).hexdigest()[:12]


def build_trajectory(rec: RunReconstruction, read: ArtifactReader) -> Trajectory:
    if rec.outcome is None:
        raise IncompleteTraceError("run has no recorded outcome")
    tenant = rec.run.tenant_id
    steps: list[NormalizedStep] = []
    file_roles: set[str] = set()
    config_shape: str | None = None
    claimed = False
    termination = "UNKNOWN"
    for event in rec.events:
        if event.type == TraceEventType.MODEL_CALL:
            tokens = int(event.metrics.get("prompt_tokens") or 0) + int(
                event.metrics.get("completion_tokens") or 0
            )
            steps.append(
                NormalizedStep(
                    index=len(steps),
                    kind="llm",
                    token="llm",
                    ok=event.status == "ok",
                    tokens=tokens,
                )
            )
        elif event.type == TraceEventType.TOOL_CALL:
            tool = event.action
            args = _load_json(read, tenant, event, "args")
            args = args if isinstance(args, dict) else {}
            role: PathRole | None = None
            if isinstance(args.get("path"), str):
                role = classify_path(args["path"])
            value_type = _value_type(args.get("value")) if tool == "set_json_value" else None
            token = f"{tool}:{role.value}" if role is not None else tool
            steps.append(
                NormalizedStep(
                    index=len(steps),
                    kind="tool",
                    token=token,
                    tool=tool,
                    role=role,
                    ok=event.status == "ok",
                    value_type=value_type,
                )
            )
            sha = _artifact(event, "output")
            if event.status == "ok" and sha is not None:
                if tool == "list_files":
                    text = read(tenant, sha).decode(errors="replace")
                    file_roles |= {classify_path(p).value for p in text.splitlines() if p.strip()}
                elif tool == "read_file" and role == PathRole.CONFIG and config_shape is None:
                    with contextlib.suppress(ValueError):
                        config_shape = _shape(json.loads(read(tenant, sha)))
        elif event.type == TraceEventType.RUN_TERMINATED:
            claimed = event.status == "claimed_done"
            termination = event.action
    if not any(e.type == TraceEventType.RUN_TERMINATED for e in rec.events):
        raise IncompleteTraceError("trace has no RUN_TERMINATED event")
    return Trajectory(
        run_id=rec.run.run_id,
        tenant_id=tenant,
        task_id=rec.run.task_id,
        task_family=rec.task.task_family,
        outcome_status=OutcomeStatus(rec.outcome.status),
        steps=steps,
        tokens_used=rec.outcome.tokens_used,
        tool_calls=rec.outcome.tool_calls,
        claimed_done=claimed,
        termination=termination,
        risk_level=rec.task.risk_level,
        allowed_tools=sorted(rec.task.allowed_tools),
        file_roles=sorted(file_roles),
        config_shape=config_shape,
    )
