"""Text protocol between the runtime and the model.

The model replies with exactly one JSON object per turn:
  {"tool": "<name>", "args": {...}} | {"final": "<summary>"} | {"escalate": "<reason>"}
"""

import json
from dataclasses import dataclass
from typing import Any

from experience_engine.ports.tools import ToolResult, ToolSpec

PROMPT_VERSION = "sys-1"
MAX_OBSERVATION_CHARS = 4000


class InvalidActionError(Exception):
    pass


@dataclass(frozen=True)
class ToolAction:
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class FinalAction:
    message: str


@dataclass(frozen=True)
class EscalateAction:
    reason: str


Action = ToolAction | FinalAction | EscalateAction


def system_prompt(tools: list[ToolSpec]) -> str:
    lines = [
        "You are a software operations agent. Investigate and fix the problem described by the "
        "user using only the tools below.",
        "Reply with exactly one JSON object per turn and nothing else:",
        '  {"tool": "<name>", "args": {...}}   to call a tool',
        '  {"final": "<short summary>"}        when you are finished',
        '  {"escalate": "<reason>"}            if a human must take over',
        "Tools:",
    ]
    for spec in tools:
        lines.append(f"- {spec.name}({json.dumps(spec.args)}): {spec.description}")
    return "\n".join(lines)


def parse_action(text: str) -> Action:
    start = text.find("{")
    if start < 0:
        raise InvalidActionError("no JSON object in reply")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise InvalidActionError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise InvalidActionError("reply is not a JSON object")
    if "final" in obj and isinstance(obj["final"], str):
        return FinalAction(obj["final"])
    if "escalate" in obj and isinstance(obj["escalate"], str):
        return EscalateAction(obj["escalate"])
    tool, args = obj.get("tool"), obj.get("args", {})
    if isinstance(tool, str) and isinstance(args, dict):
        return ToolAction(tool, args)
    raise InvalidActionError("unrecognised action shape")


def format_observation(tool: str, args: dict[str, Any], result: ToolResult) -> str:
    body = result.output
    if len(body) > MAX_OBSERVATION_CHARS:
        body = body[:MAX_OBSERVATION_CHARS] + "\n[truncated]"
    header = (
        f"OBSERVATION tool={tool} args={json.dumps(args, sort_keys=True)} "
        f"ok={str(result.ok).lower()}"
    )
    return f"{header}\n{body}"


def format_error(message: str) -> str:
    return f"ERROR {message}. Reply with exactly one JSON object."
