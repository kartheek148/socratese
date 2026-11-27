"""SQL persistence for drift-monitor transitions (M6.2). Append-only by trigger."""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.domain.drift import (
    DriftEffect,
    DriftMode,
    DriftState,
    DriftTransition,
    DriftTrigger,
)
from experience_engine.domain.ids import new_id

T = t.controller_drift_transitions


class SqlDriftLog:
    def __init__(self, engine: Engine):
        self._engine = engine

    def transitions(self, tenant_id: str, policy_id: str, version: int) -> list[DriftTransition]:
        q = (
            sa.select(T)
            .where(T.c.tenant_id == tenant_id, T.c.policy_id == policy_id, T.c.version == version)
            .order_by(T.c.cell_key, T.c.seq)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [
            DriftTransition(
                cell_key=r["cell_key"],
                seq=r["seq"],
                from_state=DriftState(r["from_state"]),
                to_state=DriftState(r["to_state"]),
                trigger=DriftTrigger(r["trigger"]),
                effect=DriftEffect(r["effect"]),
                measurements=dict(r["measurements"]),
                n_window=r["n_window"],
                decision_id=r["decision_id"],
                observed_at=r["observed_at"],
                detector_version=r["detector_version"],
                config_hash=r["config_hash"],
                mode=DriftMode(r["mode"]),
            )
            for r in rows
        ]

    def append(
        self,
        tenant_id: str,
        policy_id: str,
        version: int,
        transitions: Sequence[DriftTransition],
        now: datetime,
        config: dict[str, object],
    ) -> None:
        if not transitions:
            return
        with self._engine.begin() as conn:
            for tr in transitions:
                conn.execute(
                    T.insert().values(
                        transition_id=new_id(),
                        tenant_id=tenant_id,
                        policy_id=policy_id,
                        version=version,
                        cell_key=tr.cell_key,
                        seq=tr.seq,
                        from_state=tr.from_state.value,
                        to_state=tr.to_state.value,
                        trigger=tr.trigger.value,
                        effect=tr.effect.value,
                        mode=tr.mode.value,
                        applied=tr.applied,
                        measurements=tr.measurements,
                        n_window=tr.n_window,
                        config=config,  # the full threshold configuration behind the decision
                        config_hash=tr.config_hash,
                        detector_version=tr.detector_version,
                        decision_id=tr.decision_id,
                        observed_at=tr.observed_at,
                        created_at=now,
                    )
                )
