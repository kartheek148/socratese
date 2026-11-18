"""Simulated environment: an in-memory file tree plus a typed, allow-listed tool gateway.

No real filesystem, network or process access is possible from here.
"""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from experience_engine.ports.tools import ToolGateway, ToolNotAllowedError, ToolResult, ToolSpec

ENVIRONMENT_VERSION = "sim-env/1"


class EnvironmentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files: dict[str, str]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NoArgs(_Args):
    pass


class _PathArgs(_Args):
    path: StrictStr


class _SetJsonArgs(_Args):
    path: StrictStr
    key: StrictStr
    value: StrictStr | StrictBool | StrictInt | StrictFloat


class _WriteArgs(_Args):
    path: StrictStr
    content: StrictStr


_TOOLS: dict[str, tuple[type[_Args], str, dict[str, str]]] = {
    "list_files": (_NoArgs, "List all file paths, one per line.", {}),
    "read_file": (_PathArgs, "Read a text file.", {"path": "string"}),
    "set_json_value": (
        _SetJsonArgs,
        "Set an existing key (dotted path, e.g. a.b.c) of a JSON file to a scalar value.",
        {"path": "string", "key": "string", "value": "string|number|boolean"},
    ),
    "write_file": (
        _WriteArgs,
        "Overwrite a text file with new content.",
        {"path": "string", "content": "string"},
    ),
}


def _safe_path(path: str, files: Mapping[str, str]) -> str | None:
    norm = path.strip()
    if not norm or norm.startswith("/") or ".." in norm.split("/"):
        return None
    return norm if norm in files else None


def get_dotted(data: Any, key: str) -> tuple[bool, Any]:
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


class SimulatedToolGateway(ToolGateway):
    def __init__(self, files: dict[str, str], allowed_tools: list[str]):
        self._files = files
        self._allowed = [t for t in allowed_tools if t in _TOOLS]

    def describe(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=n, description=_TOOLS[n][1], args=_TOOLS[n][2]) for n in self._allowed
        ]

    def call(self, tool: str, args: Mapping[str, object]) -> ToolResult:
        if tool not in self._allowed:
            raise ToolNotAllowedError(f"tool '{tool[:60]}' is not allowed for this task")
        model, _, _ = _TOOLS[tool]
        try:
            parsed = model.model_validate(dict(args))
        except ValidationError as exc:
            return ToolResult(ok=False, output=f"invalid arguments: {exc.error_count()} error(s)")
        if tool == "list_files":
            return ToolResult(ok=True, output="\n".join(sorted(self._files)))
        assert isinstance(parsed, _PathArgs | _SetJsonArgs | _WriteArgs)
        path = _safe_path(parsed.path, self._files)
        if path is None:
            return ToolResult(ok=False, output=f"no such file: {parsed.path[:100]}")
        if isinstance(parsed, _PathArgs):
            return ToolResult(ok=True, output=self._files[path])
        if isinstance(parsed, _WriteArgs):
            self._files[path] = parsed.content
            return ToolResult(ok=True, output="written")
        return self._set_json(path, parsed)

    def _set_json(self, path: str, args: _SetJsonArgs) -> ToolResult:
        try:
            doc = json.loads(self._files[path])
        except json.JSONDecodeError:
            return ToolResult(ok=False, output="file is not valid JSON")
        parent_key, _, leaf = args.key.rpartition(".")
        parent = doc
        if parent_key:
            found, parent = get_dotted(doc, parent_key)
            if not found:
                return ToolResult(ok=False, output=f"key not found: {args.key}")
        if not isinstance(parent, dict) or leaf not in parent:
            return ToolResult(ok=False, output=f"key not found: {args.key}")
        parent[leaf] = args.value
        self._files[path] = json.dumps(doc, indent=2)
        return ToolResult(ok=True, output=f"set {args.key} = {json.dumps(args.value)}")


class SimulatedEnvironment:
    """One fresh, isolated environment per run; nothing is shared between sessions."""

    def __init__(self, spec: EnvironmentSpec, allowed_tools: list[str]):
        self._files = dict(spec.files)
        self._gateway = SimulatedToolGateway(self._files, allowed_tools)

    @property
    def tools(self) -> ToolGateway:
        return self._gateway

    def snapshot(self) -> Mapping[str, str]:
        return dict(self._files)
