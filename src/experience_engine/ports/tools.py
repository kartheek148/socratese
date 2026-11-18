from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    args: dict[str, str]


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    output: str


class ToolNotAllowedError(Exception):
    """The tool is not registered or not in the task's allowlist."""


class ToolGateway(Protocol):
    def describe(self) -> list[ToolSpec]:
        """Specs of the tools the task is allowed to use."""
        ...

    def call(self, tool: str, args: Mapping[str, object]) -> ToolResult:
        """Raises ToolNotAllowedError for tools outside the allowlist."""
        ...


class EnvironmentSession(Protocol):
    @property
    def tools(self) -> ToolGateway: ...

    def snapshot(self) -> Mapping[str, str]:
        """Environment state (path -> content) used as verification evidence."""
        ...
