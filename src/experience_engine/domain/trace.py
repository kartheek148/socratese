from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StepRecord(BaseModel):
    """Bounded per-step summary. Raw payloads are never stored; only size and content hash.

    Precursor of the append-only `trace_events` table introduced in milestone 2.
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
