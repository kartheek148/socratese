from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.domain.capability import (
    EXECUTABLE_STATES,
    CapabilityDefinition,
    CapabilityState,
)


class SqlExecutableCatalog:
    """Read-only capability view for task execution, scoped to one tenant per call.

    By default only ACTIVE versions are visible. Passing other states creates an *experimental*
    catalog (offline benchmarking of unpromoted capabilities): `experimental` is True and callers
    must tag every run served from it. It is never constructed by production wiring.
    """

    def __init__(self, engine: Engine, states: Iterable[CapabilityState] = EXECUTABLE_STATES):
        self._engine = engine
        self._states = frozenset(states)

    @property
    def states(self) -> frozenset[CapabilityState]:
        return self._states

    @property
    def experimental(self) -> bool:
        return self._states != EXECUTABLE_STATES

    def _select(self, tenant_id: str) -> sa.Select[tuple[object, ...]]:
        cv = t.capability_versions
        return sa.select(cv.c.definition).where(
            cv.c.tenant_id == tenant_id, cv.c.state.in_([s.value for s in self._states])
        )

    def list_executable(self, tenant_id: str) -> list[CapabilityDefinition]:
        cv = t.capability_versions
        with self._engine.connect() as conn:
            rows = conn.execute(
                self._select(tenant_id).order_by(cv.c.capability_id, cv.c.version)
            ).scalars()
            return [CapabilityDefinition.model_validate(d) for d in rows]

    def get_executable(
        self, tenant_id: str, capability_id: str, version: int
    ) -> CapabilityDefinition | None:
        cv = t.capability_versions
        with self._engine.connect() as conn:
            row = conn.execute(
                self._select(tenant_id).where(
                    cv.c.capability_id == capability_id, cv.c.version == version
                )
            ).scalar_one_or_none()
        return CapabilityDefinition.model_validate(row) if row is not None else None
