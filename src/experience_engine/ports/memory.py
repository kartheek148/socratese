"""Ports for learned memory: candidate registration (learning plane) and the read-only catalog of
ACTIVE versions (execution plane). The two are deliberately different objects."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from experience_engine.domain.capability import CapabilityState
from experience_engine.domain.memory import MemoryDefinition


@dataclass(frozen=True)
class LatestMemory:
    version: int
    content_hash: str
    state: CapabilityState


class MemoryStore(Protocol):
    """Learning-plane writer: it can only create CANDIDATE versions, never change state."""

    def latest(self, tenant_id: str, memory_id: str) -> LatestMemory | None: ...

    def register(self, definition: MemoryDefinition) -> bool:
        """Insert an immutable CANDIDATE version. False if (tenant, id, version) already exists."""
        ...

    def list_versions(
        self, tenant_id: str, states: frozenset[CapabilityState] | None = None
    ) -> list[tuple[MemoryDefinition, CapabilityState]]: ...


class ActiveMemoryCatalog(Protocol):
    """Execution-plane reader. Returns only versions in an executable state (ACTIVE), for one
    tenant. There is no method that can return CANDIDATE/VALIDATING/REJECTED content."""

    def list_active(self, tenant_id: str) -> Sequence[MemoryDefinition]: ...
