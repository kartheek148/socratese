from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.asset_spec import AssetSpec, capability_spec
from experience_engine.adapters.db.run_lifecycle import write_audit
from experience_engine.domain.capability import CapabilityDefinition, CapabilityState
from experience_engine.domain.evaluation import EvaluationReport, Suite, Verdict
from experience_engine.domain.ids import new_id
from experience_engine.domain.trace import ArtifactRef
from experience_engine.ports.registry import (
    EvaluationSummary,
    ObservationStats,
    Pointer,
    StaleStateError,
    TransitionSpec,
    VersionRecord,
)


class SqlAssetRegistryStore[D]:
    """Registry persistence for one asset kind (see `AssetSpec`). Capability behaviour is the
    pre-M5.8 behaviour; memory uses the same code over its own tables."""

    def __init__(self, engine: Engine, spec: AssetSpec[D]):
        self._engine = engine
        self._spec = spec

    def get_version(
        self, tenant_id: str, capability_id: str, version: int
    ) -> VersionRecord[D] | None:
        cv, idc = self._spec.versions, self._spec.id_col
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(cv.c.definition, cv.c.state).where(
                    cv.c.tenant_id == tenant_id,
                    cv.c[idc] == capability_id,
                    cv.c.version == version,
                )
            ).first()
        if row is None:
            return None
        return VersionRecord(
            tenant_id, capability_id, version, CapabilityState(row[1]), self._spec.decode(row[0])
        )

    def get_pointer(self, tenant_id: str, capability_id: str) -> Pointer:
        p, idc = self._spec.pointers, self._spec.id_col
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(p.c.active_version, p.c.previous_version).where(
                    p.c.tenant_id == tenant_id, p.c[idc] == capability_id
                )
            ).first()
        return Pointer(row[0], row[1]) if row else Pointer()

    def apply_transitions(
        self,
        *,
        tenant_id: str,
        capability_id: str,
        transitions: list[TransitionSpec],
        pointer: Pointer | None,
        actor: str,
        reason: str,
        evaluation_id: str | None,
        policy_hash: str | None,
        now: datetime,
    ) -> None:
        # Leave ACTIVE before entering ACTIVE so the one-active-per-capability index holds.
        ordered = sorted(transitions, key=lambda tr: tr.to_state == CapabilityState.ACTIVE)
        sp = self._spec
        cv, p, idc = sp.versions, sp.pointers, sp.id_col
        with self._engine.begin() as conn:
            known = conn.execute(
                sa.select(sp.identity.c[idc]).where(
                    sp.identity.c.tenant_id == tenant_id, sp.identity.c[idc] == capability_id
                )
            ).scalar_one_or_none()
            if known is None:
                raise StaleStateError(f"asset {capability_id} not found for this tenant")
            conn.execute(
                pg_insert(p)
                .values(**{"tenant_id": tenant_id, idc: capability_id, "updated_at": now})
                .on_conflict_do_nothing(index_elements=["tenant_id", idc])
            )
            conn.execute(  # serialize concurrent lifecycle changes of one asset
                sa.select(p.c[idc])
                .where(p.c.tenant_id == tenant_id, p.c[idc] == capability_id)
                .with_for_update()
            )
            for tr in ordered:
                current = conn.execute(
                    sa.select(cv.c.state)
                    .where(
                        cv.c.tenant_id == tenant_id,
                        cv.c[idc] == capability_id,
                        cv.c.version == tr.version,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if current != tr.expected_from.value:
                    raise StaleStateError(
                        f"{capability_id} v{tr.version} is {current}, "
                        f"expected {tr.expected_from.value}"
                    )
                conn.execute(
                    cv.update()
                    .where(
                        cv.c.tenant_id == tenant_id,
                        cv.c[idc] == capability_id,
                        cv.c.version == tr.version,
                    )
                    .values(state=tr.to_state.value)
                )
                conn.execute(
                    sp.transitions.insert().values(
                        **{idc: capability_id},
                        transition_id=new_id(),
                        tenant_id=tenant_id,
                        version=tr.version,
                        from_state=tr.expected_from.value,
                        to_state=tr.to_state.value,
                        actor=actor,
                        reason=reason,
                        evaluation_id=evaluation_id,
                        policy_hash=policy_hash,
                        created_at=now,
                    )
                )
                write_audit(
                    conn,
                    tenant_id=tenant_id,
                    action=sp.audit_action,
                    entity_type=sp.entity_type,
                    entity_id=f"{capability_id}@{tr.version}",
                    correlation_id=capability_id,
                    ts=now,
                    actor=actor,
                    detail={
                        "from": tr.expected_from.value,
                        "to": tr.to_state.value,
                        "reason": reason[:200],
                        "policy_hash": policy_hash,
                    },
                )
            if pointer is not None:
                conn.execute(
                    p.update()
                    .where(p.c.tenant_id == tenant_id, p.c[idc] == capability_id)
                    .values(
                        active_version=pointer.active_version,
                        previous_version=pointer.previous_version,
                        updated_at=now,
                    )
                )

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

    def record_evaluation(self, report: EvaluationReport, report_sha256: str, now: datetime) -> str:
        evaluation_id = new_id()
        reg = report.suites.get(Suite.REGRESSION.value, {}).get(report.treatment_arm)
        with self._engine.begin() as conn:
            conn.execute(
                self._spec.evaluations.insert().values(
                    **{self._spec.id_col: report.capability_id},
                    evaluation_id=evaluation_id,
                    tenant_id=report.tenant_id,
                    version=report.version,
                    verdict=report.verdict.value,
                    risk_class=report.risk_class,
                    policy_name=report.policy_name,
                    policy_hash=report.policy_hash,
                    policy=report.policy.model_dump(mode="json"),
                    source_replay_n=report.n_in(Suite.SOURCE_REPLAY),
                    heldout_n=report.n_in(Suite.HELD_OUT),
                    transfer_n=report.n_in(Suite.TRANSFER),
                    negative_n=report.n_in(Suite.NEGATIVE),
                    regression_n=reg.n if reg else 0,
                    # per-task rows stay out of the relational record; only aggregates are queryable
                    metrics={
                        "suites": {
                            s: {a: m.model_dump(mode="json") for a, m in arms.items()}
                            for s, arms in report.suites.items()
                        },
                        "false_applicability_rate": report.false_applicability_rate,
                        "regressions": report.regressions,
                        "success_lower_bound": report.success_lower_bound,
                        "lifecycle": report.lifecycle.model_dump(mode="json"),
                    },
                    reasons=report.reasons,
                    dataset_versions=report.dataset_versions,
                    model_settings=report.model_settings,
                    seeds=report.seeds,
                    runtime_version=report.runtime_version,
                    verifier_version=report.verifier_version,
                    environment_version=report.environment_version,
                    evaluator_version=report.evaluator_version,
                    report_sha256=report_sha256,
                    started_at=report.started_at,
                    completed_at=report.completed_at,
                )
            )
        return evaluation_id

    def latest_evaluation(
        self, tenant_id: str, capability_id: str, version: int
    ) -> EvaluationSummary | None:
        e, idc = self._spec.evaluations, self._spec.id_col
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(e.c.evaluation_id, e.c.verdict, e.c.policy_hash, e.c.risk_class)
                .where(
                    e.c.tenant_id == tenant_id,
                    e.c[idc] == capability_id,
                    e.c.version == version,
                )
                .order_by(e.c.completed_at.desc(), e.c.evaluation_id.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return EvaluationSummary(row[0], Verdict(row[1]), row[2], row[3])

    def record_observation(
        self,
        *,
        tenant_id: str,
        capability_id: str,
        version: int,
        mode: str,
        task_id: str,
        capability_verified: bool,
        baseline_verified: bool | None,
        capability_tokens: int,
        baseline_tokens: int | None,
        now: datetime,
    ) -> None:
        with self._engine.begin() as conn:
            sp = self._spec
            conn.execute(
                sp.observations.insert().values(
                    **{
                        sp.id_col: capability_id,
                        sp.verified_col: capability_verified,
                        sp.tokens_col: capability_tokens,
                    },
                    observation_id=new_id(),
                    tenant_id=tenant_id,
                    version=version,
                    mode=mode,
                    task_id=task_id,
                    baseline_verified=baseline_verified,
                    baseline_tokens=baseline_tokens,
                    created_at=now,
                )
            )

    def observation_stats(
        self, tenant_id: str, capability_id: str, version: int, mode: str
    ) -> ObservationStats:
        o, idc, vcol = self._spec.observations, self._spec.id_col, self._spec.verified_col
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.count().filter(o.c[vcol].is_(True)),
                    sa.func.count().filter(o.c.baseline_verified.is_not(None)),
                    sa.func.count().filter(
                        sa.and_(o.c.baseline_verified.is_(True), o.c[vcol].is_(False))
                    ),
                ).where(
                    o.c.tenant_id == tenant_id,
                    o.c[idc] == capability_id,
                    o.c.version == version,
                    o.c.mode == mode,
                )
            ).one()
        return ObservationStats(int(row[0]), int(row[1]), int(row[2]), int(row[3]))


class SqlRegistryStore(SqlAssetRegistryStore[CapabilityDefinition]):
    """Capability registry persistence (unchanged behaviour)."""

    def __init__(self, engine: Engine):
        super().__init__(
            engine, capability_spec(lambda raw: CapabilityDefinition.model_validate(raw))
        )
