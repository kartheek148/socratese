from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from experience_engine.adapters.db import tables as t
from experience_engine.domain.experience import ExperienceCase
from experience_engine.domain.ids import new_id
from experience_engine.domain.trace import ArtifactRef


class SqlExperienceStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_case(self, tenant_id: str, run_id: str, pipeline_version: str) -> ExperienceCase | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(t.experience_cases).where(
                        t.experience_cases.c.tenant_id == tenant_id,
                        t.experience_cases.c.run_id == run_id,
                        t.experience_cases.c.pipeline_version == pipeline_version,
                    )
                )
                .mappings()
                .first()
            )
        return ExperienceCase.model_validate(dict(row)) if row else None

    def save_case(self, case: ExperienceCase) -> bool:
        values = case.model_dump(mode="json")
        values["excluded_reason"] = case.excluded_reason.value if case.excluded_reason else None
        values["outcome_status"] = case.outcome_status.value
        values["created_at"] = case.created_at
        with self._engine.begin() as conn:
            res = conn.execute(
                pg_insert(t.experience_cases)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "run_id", "pipeline_version"])
            )
            return bool(res.rowcount == 1)

    def register_artifact(self, tenant_id: str, ref: ArtifactRef, uri: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(t.artifacts)
                .values(
                    tenant_id=tenant_id,
                    sha256=ref.sha256,
                    uri=uri,
                    size_bytes=ref.size_bytes,
                    content_type=ref.content_type,
                    redacted=ref.redacted,
                    truncated=ref.truncated,
                    created_at=sa.func.now(),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "sha256"])
            )

    def list_eligible_cases(self, tenant_id: str, pipeline_version: str) -> list[ExperienceCase]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.select(t.experience_cases)
                    .where(
                        t.experience_cases.c.tenant_id == tenant_id,
                        t.experience_cases.c.pipeline_version == pipeline_version,
                        t.experience_cases.c.eligible.is_(True),
                    )
                    .order_by(t.experience_cases.c.run_id)
                )
                .mappings()
                .all()
            )
        return [ExperienceCase.model_validate(dict(r)) for r in rows]

    def record_cost(
        self,
        *,
        tenant_id: str,
        pipeline: str,
        subject_type: str,
        subject_id: str,
        tokens: int,
        tool_calls: int,
        wall_ms: int,
        correlation_id: str,
        now: datetime,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                t.cost_ledger.insert().values(
                    entry_id=new_id(),
                    tenant_id=tenant_id,
                    pipeline=pipeline,
                    subject_type=subject_type,
                    subject_id=subject_id[:200],
                    tokens=tokens,
                    tool_calls=tool_calls,
                    wall_ms=wall_ms,
                    correlation_id=correlation_id,
                    created_at=now,
                )
            )
