"""Execution-plane view of memory: ACTIVE versions only, one tenant per call.

Mirrors `SqlExecutableCatalog`. Passing other states creates an *experimental* catalog for offline
research; `experimental` is then True and every consumer must tag its results. Production wiring
never builds one.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.domain.capability import EXECUTABLE_STATES, CapabilityState
from experience_engine.domain.memory import MemoryDefinition


class SqlActiveMemoryCatalog:
    def __init__(self, engine: Engine, states: Iterable[CapabilityState] = EXECUTABLE_STATES):
        self._engine = engine
        self._states = frozenset(states)

    @property
    def experimental(self) -> bool:
        return self._states != EXECUTABLE_STATES

    @property
    def states(self) -> frozenset[CapabilityState]:
        return self._states

    def list_active(self, tenant_id: str) -> Sequence[MemoryDefinition]:
        mv = t.memory_versions
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(mv.c.definition)
                .where(
                    mv.c.tenant_id == tenant_id,
                    mv.c.state.in_([s.value for s in self._states]),
                )
                .order_by(mv.c.memory_id, mv.c.version)
            ).scalars()
            return [MemoryDefinition.model_validate(d) for d in rows]


class StaticMemoryCatalog:
    """In-memory catalog (tests and offline experiments); same tenant scoping."""

    def __init__(self, tenant_id: str, definitions: Sequence[MemoryDefinition]):
        self._tenant = tenant_id
        self._defs = list(definitions)

    def list_active(self, tenant_id: str) -> Sequence[MemoryDefinition]:
        return list(self._defs) if tenant_id == self._tenant else []
