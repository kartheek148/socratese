"""SQL persistence for revalidation transitions and trial evidence (M6.3); append-only."""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.domain.ids import new_id
from experience_engine.domain.revalidation import (
    QuarantineReason,
    RejectReason,
    RevalState,
    RevalStrategy,
    RevalTransition,
    RevalTrial,
    RevalTrigger,
    TrialKey,
    TrialResult,
    TrialStage,
    TrialVerdict,
)

TR = t.controller_revalidation_transitions
TT = t.controller_revalidation_trials


class SqlRevalidationLog:
    def __init__(self, engine: Engine):
        self._engine = engine

    def transitions(self, tenant_id: str, policy_id: str, version: int) -> list[RevalTransition]:
        q = (
            sa.select(TR)
            .where(
                TR.c.tenant_id == tenant_id, TR.c.policy_id == policy_id, TR.c.version == version
            )
            .order_by(TR.c.cell_key, TR.c.seq)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [
            RevalTransition(
                cell_key=r["cell_key"],
                episode=r["episode"],
                cycle=r["cycle"],
                seq=r["seq"],
                from_state=RevalState(r["from_state"]) if r["from_state"] else None,
                to_state=RevalState(r["to_state"]),
                trigger=RevalTrigger(r["trigger"]),
                reason=QuarantineReason(r["reason"]),
                strategy=RevalStrategy(r["strategy"]),
                measurements=dict(r["measurements"]),
                detail=list(r["detail"]),
                facts=dict(r["facts"]),
                rebase=dict(r["rebase"]),
                n_trials=r["n_trials"],
                n_unverified=r["n_unverified"],
                tokens_cycle=r["tokens_cycle"],
                anchor=r["anchor"],
                observed_at=r["observed_at"],
                params_hash=r["params_hash"],
                applied=r["applied"],
                version=r["log_version"],
            )
            for r in rows
        ]

    def trials(self, tenant_id: str, policy_id: str, version: int) -> list[RevalTrial]:
        q = (
            sa.select(TT)
            .where(
                TT.c.tenant_id == tenant_id, TT.c.policy_id == policy_id, TT.c.version == version
            )
            .order_by(TT.c.cell_key, TT.c.episode, TT.c.cycle, TT.c.stage, TT.c.ordinal)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [
            RevalTrial(
                key=TrialKey(
                    cell_key=r["cell_key"],
                    episode=r["episode"],
                    cycle=r["cycle"],
                    ordinal=r["ordinal"],
                    stage=TrialStage(r["stage"]),
                ),
                result=TrialResult(
                    verdict=TrialVerdict(r["verdict"]),
                    tokens=r["tokens"],
                    task_ref=r["task_ref"],
                    observed_at=r["observed_at"],
                    runtime_version=r["runtime_version"],
                    verifier_version=r["verifier_version"],
                    capability_ref=r["capability_ref"],
                    latency_ms=r["latency_ms"],
                    tenant_id=r["result_tenant_id"],
                ),
                anchor=r["anchor"],
                round_at=r["round_at"],
                accepted=r["accepted"],
                reject_reason=RejectReason(r["reject_reason"]) if r["reject_reason"] else None,
            )
            for r in rows
        ]

    def append(
        self,
        tenant_id: str,
        policy_id: str,
        version: int,
        transitions: Sequence[RevalTransition],
        trials: Sequence[RevalTrial],
        now: datetime,
        config: dict[str, object],
    ) -> None:
        if not transitions and not trials:
            return
        with self._engine.begin() as conn:  # one transaction: evidence and its consequences
            for tr in trials:
                conn.execute(
                    TT.insert().values(
                        trial_id=new_id(),
                        tenant_id=tenant_id,
                        policy_id=policy_id,
                        version=version,
                        cell_key=tr.key.cell_key,
                        episode=tr.key.episode,
                        cycle=tr.key.cycle,
                        stage=tr.key.stage.value,
                        ordinal=tr.key.ordinal,
                        anchor=tr.anchor,
                        round_at=tr.round_at,
                        task_ref=tr.result.task_ref,
                        verdict=tr.result.verdict.value,
                        tokens=tr.result.tokens,
                        latency_ms=tr.result.latency_ms,
                        runtime_version=tr.result.runtime_version,
                        verifier_version=tr.result.verifier_version,
                        capability_ref=tr.result.capability_ref,
                        result_tenant_id=tr.result.tenant_id,
                        observed_at=tr.result.observed_at,
                        accepted=tr.accepted,
                        reject_reason=tr.reject_reason.value if tr.reject_reason else None,
                        created_at=now,
                    )
                )
            for x in transitions:
                conn.execute(
                    TR.insert().values(
                        transition_id=new_id(),
                        tenant_id=tenant_id,
                        policy_id=policy_id,
                        version=version,
                        cell_key=x.cell_key,
                        episode=x.episode,
                        cycle=x.cycle,
                        seq=x.seq,
                        from_state=x.from_state.value if x.from_state else None,
                        to_state=x.to_state.value,
                        trigger=x.trigger.value,
                        reason=x.reason.value,
                        strategy=x.strategy.value,
                        measurements=x.measurements,
                        detail=x.detail,
                        facts=x.facts,
                        rebase=x.rebase,
                        n_trials=x.n_trials,
                        n_unverified=x.n_unverified,
                        tokens_cycle=x.tokens_cycle,
                        anchor=x.anchor,
                        observed_at=x.observed_at,
                        params_hash=x.params_hash,
                        config=config,
                        applied=x.applied,
                        log_version=x.version,
                        created_at=now,
                    )
                )
