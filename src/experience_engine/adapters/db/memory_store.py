"""Learning-plane memory persistence: registers CANDIDATE versions only. There is no code path
here that changes state; the registry store is the only writer of lifecycle changes."""

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from experience_engine.adapters.db import tables as t
from experience_engine.domain.capability import CapabilityState
from experience_engine.domain.ids import new_id
from experience_engine.domain.memory import MemoryDefinition
from experience_engine.ports.memory import LatestMemory

mv = t.memory_versions


class SqlMemoryStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def latest(self, tenant_id: str, memory_id: str) -> LatestMemory | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(mv.c.version, mv.c.content_hash, mv.c.state)
                .where(mv.c.tenant_id == tenant_id, mv.c.memory_id == memory_id)
                .order_by(mv.c.version.desc())
                .limit(1)
            ).first()
        return None if row is None else LatestMemory(row[0], row[1], CapabilityState(row[2]))

    def register(self, definition: MemoryDefinition) -> bool:
        d = definition
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(t.memories)
                .values(
                    tenant_id=d.tenant_id,
                    memory_id=d.memory_id,
                    memory_type=d.memory_type.value,
                    task_family=d.task_family,
                    created_at=d.created_at,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "memory_id"])
            )
            res = conn.execute(
                pg_insert(mv)
                .values(
                    version_id=new_id(),
                    tenant_id=d.tenant_id,
                    memory_id=d.memory_id,
                    version=d.version,
                    state=CapabilityState.CANDIDATE.value,
                    definition=d.model_dump(mode="json"),
                    content_hash=d.content_hash,
                    created_at=d.created_at,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "memory_id", "version"])
            )
        return bool(res.rowcount)

    def list_versions(
        self, tenant_id: str, states: frozenset[CapabilityState] | None = None
    ) -> list[tuple[MemoryDefinition, CapabilityState]]:
        query = sa.select(mv.c.definition, mv.c.state).where(mv.c.tenant_id == tenant_id)
        if states is not None:
            query = query.where(mv.c.state.in_([s.value for s in states]))
        with self._engine.connect() as conn:
            rows = conn.execute(query.order_by(mv.c.memory_id, mv.c.version)).all()
        return [(MemoryDefinition.model_validate(d), CapabilityState(s)) for d, s in rows]
