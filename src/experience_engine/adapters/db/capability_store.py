from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from experience_engine.adapters.db import tables as t
from experience_engine.domain.capability import (
    EXECUTABLE_STATES,
    CandidateBundle,
    CapabilityDefinition,
    CapabilityState,
    RegistrationKind,
    definition_hash,
)
from experience_engine.domain.ids import new_id
from experience_engine.ports.experience import RegistrationResult


class SqlCapabilityStore:
    """Registers capabilities as CANDIDATE only; this adapter has no code path that promotes a
    version. `list_executable` is the single view normal execution may use and returns only
    ACTIVE versions.
    """

    def __init__(self, engine: Engine):
        self._engine = engine

    def register_candidate(
        self,
        tenant_id: str,
        bundle: CandidateBundle,
        source_run_ids: list[str],
        counterexample_run_ids: list[str],
        now: datetime,
    ) -> RegistrationResult:
        defn = bundle.definition
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(t.capabilities)
                .values(
                    tenant_id=tenant_id,
                    capability_id=defn.id,
                    task_family=bundle.task_family,
                    skeleton_signature=bundle.skeleton_signature,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "capability_id"])
            )
            # Serialize concurrent registrations of the same capability.
            conn.execute(
                sa.select(t.capabilities.c.capability_id)
                .where(
                    t.capabilities.c.tenant_id == tenant_id,
                    t.capabilities.c.capability_id == defn.id,
                )
                .with_for_update()
            )
            latest = (
                conn.execute(
                    sa.select(
                        t.capability_versions.c.version, t.capability_versions.c.procedure_hash
                    )
                    .where(
                        t.capability_versions.c.tenant_id == tenant_id,
                        t.capability_versions.c.capability_id == defn.id,
                    )
                    .order_by(t.capability_versions.c.version.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if latest is not None and latest["procedure_hash"] == bundle.procedure_hash:
                added = self._add_evidence(
                    conn,
                    tenant_id,
                    defn.id,
                    latest["version"],
                    source_run_ids,
                    counterexample_run_ids,
                    now,
                )
                kind = RegistrationKind.EVIDENCE_ADDED if added else RegistrationKind.NO_CHANGE
                return RegistrationResult(kind, defn.id, latest["version"], added)

            version = 1 if latest is None else latest["version"] + 1
            versioned = defn.model_copy(update={"version": version})
            conn.execute(
                t.capability_versions.insert().values(
                    version_id=new_id(),
                    tenant_id=tenant_id,
                    capability_id=defn.id,
                    version=version,
                    state=CapabilityState.CANDIDATE.value,
                    definition=versioned.model_dump(mode="json"),
                    definition_hash=definition_hash(versioned),
                    procedure_hash=bundle.procedure_hash,
                    tests=[x.model_dump(mode="json") for x in bundle.tests],
                    compiler_version=defn.provenance.compiler_version,
                    prompt_version=defn.provenance.prompt_version,
                    model_version=defn.provenance.model_version,
                    created_at=now,
                )
            )
            added = self._add_evidence(
                conn, tenant_id, defn.id, version, source_run_ids, counterexample_run_ids, now
            )
            kind = (
                RegistrationKind.NEW_CAPABILITY if latest is None else RegistrationKind.NEW_VERSION
            )
            return RegistrationResult(kind, defn.id, version, added)

    @staticmethod
    def _add_evidence(
        conn: sa.Connection,
        tenant_id: str,
        capability_id: str,
        version: int,
        sources: list[str],
        counterexamples: list[str],
        now: datetime,
    ) -> int:
        added = 0
        for role, run_ids in (("SOURCE_SUCCESS", sources), ("COUNTEREXAMPLE", counterexamples)):
            for run_id in run_ids:
                res = conn.execute(
                    pg_insert(t.capability_evidence)
                    .values(
                        evidence_id=new_id(),
                        tenant_id=tenant_id,
                        capability_id=capability_id,
                        version=version,
                        run_id=run_id,
                        role=role,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_capability_evidence_key")
                    .returning(t.capability_evidence.c.evidence_id)
                )
                added += int(res.first() is not None)
        return added

    def list_versions(
        self, tenant_id: str, states: frozenset[CapabilityState] | None = None
    ) -> list[tuple[CapabilityDefinition, CapabilityState]]:
        cv = t.capability_versions
        query = sa.select(cv.c.definition, cv.c.state).where(cv.c.tenant_id == tenant_id)
        if states is not None:
            query = query.where(cv.c.state.in_([s.value for s in states]))
        with self._engine.connect() as conn:
            rows = conn.execute(query.order_by(cv.c.capability_id, cv.c.version)).all()
        return [(CapabilityDefinition.model_validate(d), CapabilityState(s)) for d, s in rows]

    def list_executable(self, tenant_id: str) -> list[CapabilityDefinition]:
        return [d for d, _ in self.list_versions(tenant_id, EXECUTABLE_STATES)]
