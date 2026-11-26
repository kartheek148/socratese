"""SQL persistence for controller policy versions, rollout evidence and the decision log."""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.run_lifecycle import write_audit
from experience_engine.domain.controller import (
    CONTROLLER_KEY,
    PLANNING_KEY,
    CandidateFeature,
    ControllerPolicyDefinition,
    DecisionFeatures,
    DecisionOutcomeRecord,
    action_key,
)
from experience_engine.domain.decision import ControllerDecision, Strategy
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.ids import new_id
from experience_engine.domain.outcome import OutcomeStatus
from experience_engine.domain.rollout import PolicyState, Stage, StageReport
from experience_engine.ports.controller_store import (
    PolicyRecord,
    PolicyTransition,
    StageEvidence,
    TransitionRecord,
)
from experience_engine.ports.registry import StaleStateError

C = t.controller_policies


def _record(row: sa.Row[tuple[object, ...]]) -> PolicyRecord:
    m = row._mapping
    return PolicyRecord(
        tenant_id=m["tenant_id"],
        policy_id=m["policy_id"],
        version=m["version"],
        definition=ControllerPolicyDefinition.model_validate(m["definition"]),
        definition_hash=m["definition_hash"],
        state=PolicyState(m["state"]),
        created_at=m["created_at"],
    )


class SqlControllerPolicyStore:
    def __init__(self, engine: Engine):
        self._engine = engine

    def add_version(self, definition: ControllerPolicyDefinition, now: datetime) -> PolicyRecord:
        with self._engine.begin() as conn:
            conn.execute(
                C.insert().values(
                    version_id=new_id(),
                    tenant_id=definition.tenant_id,
                    policy_id=definition.policy_id,
                    version=definition.version,
                    kind=definition.kind.value,
                    state=PolicyState.CANDIDATE.value,
                    definition=definition.model_dump(mode="json"),
                    definition_hash=definition.definition_hash(),
                    created_at=now,
                )
            )
            write_audit(
                conn,
                tenant_id=definition.tenant_id,
                action="controller_policy.registered",
                entity_type="controller_policy",
                entity_id=definition.policy_version,
                correlation_id=definition.policy_id,
                ts=now,
                detail={
                    "definition_hash": definition.definition_hash(),
                    "kind": definition.kind.value,
                },
            )
        return PolicyRecord(
            definition.tenant_id,
            definition.policy_id,
            definition.version,
            definition,
            definition.definition_hash(),
            PolicyState.CANDIDATE,
            now,
        )

    def get_version(self, tenant_id: str, policy_id: str, version: int) -> PolicyRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(C).where(
                    C.c.tenant_id == tenant_id, C.c.policy_id == policy_id, C.c.version == version
                )
            ).first()
        return _record(row) if row else None

    def list_versions(self, tenant_id: str, policy_id: str) -> list[PolicyRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(C)
                .where(C.c.tenant_id == tenant_id, C.c.policy_id == policy_id)
                .order_by(C.c.version)
            ).all()
        return [_record(r) for r in rows]

    def add_evaluation(
        self, tenant_id: str, policy_id: str, version: int, report: StageReport, now: datetime
    ) -> str:
        eid = new_id()
        with self._engine.begin() as conn:
            conn.execute(
                t.controller_policy_evaluations.insert().values(
                    evaluation_id=eid,
                    tenant_id=tenant_id,
                    policy_id=policy_id,
                    version=version,
                    stage=report.stage.value,
                    verdict=report.verdict.value,
                    definition_hash=report.definition_hash,
                    gate_hash=report.gate_hash,
                    metrics=report.metrics,
                    reasons=report.reasons,
                    created_at=now,
                )
            )
        return eid

    def latest_evaluation(
        self, tenant_id: str, policy_id: str, version: int, stage: Stage
    ) -> StageEvidence | None:
        e = t.controller_policy_evaluations
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(e)
                .where(
                    e.c.tenant_id == tenant_id,
                    e.c.policy_id == policy_id,
                    e.c.version == version,
                    e.c.stage == stage.value,
                )
                .order_by(e.c.created_at.desc(), e.c.evaluation_id.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        m = row._mapping
        return StageEvidence(
            m["evaluation_id"],
            StageReport(
                stage=Stage(m["stage"]),
                verdict=Verdict(m["verdict"]),
                policy_version=f"{policy_id}/{version}",
                definition_hash=m["definition_hash"],
                gate_hash=m["gate_hash"],
                metrics=m["metrics"],
                reasons=m["reasons"],
            ),
            m["created_at"],
        )

    def apply_transitions(
        self,
        tenant_id: str,
        policy_id: str,
        transitions: Sequence[PolicyTransition],
        actor: str,
        reason: str,
        evaluation_id: str | None,
        now: datetime,
    ) -> None:
        # leave ACTIVE before entering ACTIVE so the one-active-per-policy index holds
        ordered = sorted(transitions, key=lambda tr: tr.to_state == PolicyState.ACTIVE)
        with self._engine.begin() as conn:
            conn.execute(  # serialise concurrent lifecycle changes of one policy
                sa.select(C.c.version)
                .where(C.c.tenant_id == tenant_id, C.c.policy_id == policy_id)
                .with_for_update()
            ).all()
            for tr in ordered:
                current = conn.execute(
                    sa.select(C.c.state).where(
                        C.c.tenant_id == tenant_id,
                        C.c.policy_id == policy_id,
                        C.c.version == tr.version,
                    )
                ).scalar_one_or_none()
                if current != tr.from_state.value:
                    raise StaleStateError(
                        f"{policy_id}/{tr.version} is {current}, expected {tr.from_state.value}"
                    )
                conn.execute(
                    C.update()
                    .where(
                        C.c.tenant_id == tenant_id,
                        C.c.policy_id == policy_id,
                        C.c.version == tr.version,
                    )
                    .values(state=tr.to_state.value)
                )
                conn.execute(
                    t.controller_policy_transitions.insert().values(
                        transition_id=new_id(),
                        tenant_id=tenant_id,
                        policy_id=policy_id,
                        version=tr.version,
                        from_state=tr.from_state.value,
                        to_state=tr.to_state.value,
                        actor=actor,
                        reason=reason,
                        evaluation_id=evaluation_id,
                        created_at=now,
                    )
                )
                write_audit(
                    conn,
                    tenant_id=tenant_id,
                    action="controller_policy.transition",
                    entity_type="controller_policy",
                    entity_id=f"{policy_id}/{tr.version}",
                    correlation_id=policy_id,
                    ts=now,
                    actor=actor,
                    detail={
                        "from": tr.from_state.value,
                        "to": tr.to_state.value,
                        "reason": reason[:200],
                    },
                )

    def history(self, tenant_id: str, policy_id: str) -> list[TransitionRecord]:
        tr = t.controller_policy_transitions
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(tr)
                .where(tr.c.tenant_id == tenant_id, tr.c.policy_id == policy_id)
                .order_by(tr.c.created_at, tr.c.transition_id)
            ).all()
        return [
            TransitionRecord(
                r.policy_id,
                r.version,
                PolicyState(r.from_state),
                PolicyState(r.to_state),
                r.actor,
                r.reason,
                r.evaluation_id,
                r.created_at,
            )
            for r in rows
        ]


# ---- decision log (learning plane read side) ----------------------------------------------------
PD = t.policy_decisions
RO = t.run_outcomes


def _actions_from_snapshot(
    snap: dict[str, object], selected: str
) -> tuple[list[CandidateFeature], str]:
    """Reconstruct the feasible set and rule default of a pre-adaptive (rule) decision."""
    acts = [CandidateFeature(key=PLANNING_KEY, strategy=Strategy.GENERAL_PLANNING)]
    raw = snap.get("candidates")
    if isinstance(raw, list):
        eligible = [c for c in raw if isinstance(c, dict) and c.get("eligible") is True]
        for c in sorted(
            eligible, key=lambda c: (-float(c.get("similarity", 0.0)), str(c.get("capability")))
        ):
            ref = str(c["capability"])
            strategy = (
                Strategy.COMPOSE_CAPABILITIES
                if c.get("strategy") == Strategy.COMPOSE_CAPABILITIES.value
                else Strategy.DIRECT_CAPABILITY
            )
            acts.append(
                CandidateFeature(
                    key=action_key(strategy, ref),
                    strategy=strategy,
                    ref=ref,
                    similarity=float(c.get("similarity", 0.0)),
                )
            )
    default = acts[1].key if len(acts) > 1 else PLANNING_KEY
    return acts, default if len(acts) > 1 else selected


def _decision_from_row(m: sa.RowMapping) -> ControllerDecision:
    return ControllerDecision.model_validate(
        {
            "decision_id": m["decision_id"],
            "run_id": m["run_id"],
            "tenant_id": m["tenant_id"],
            "policy_version": m["policy_version"],
            "feature_snapshot": m["feature_snapshot"],
            "eligible_actions": m["eligible_actions"],
            "estimates": m["estimates"],
            "selected_action": m["selected_action"],
            "selection_probability": m["selection_probability"],
            "reason": m["reason"],
            "outcome_status": m["outcome_status"],
            "acted": m["acted"],
        }
    )


def _record_of(m: sa.RowMapping) -> DecisionOutcomeRecord:
    snap: dict[str, object] = m["feature_snapshot"]
    selected = Strategy(m["selected_action"])
    ref = snap.get("selected_capability")
    key = action_key(selected, ref if isinstance(ref, str) else None)
    raw = snap.get(CONTROLLER_KEY)
    if isinstance(raw, dict):
        f = DecisionFeatures.model_validate(raw)
        actions, default = f.actions, f.default_key
        pis = {
            k[3:]: v
            for k, v in (m["estimates"] or {}).items()
            if isinstance(k, str) and k.startswith("pi:")
        }
        propensities = pis or {key: m["selection_probability"] or 1.0}
        risk = f.risk_level
        family = f.task_family
        n_tools = f.n_allowed_tools
    else:
        actions, default = _actions_from_snapshot(snap, key)
        propensities = {key: m["selection_probability"] or 1.0}
        risk = snap.get("risk_level", "low")  # type: ignore[assignment]
        fam = snap.get("task_family")
        family = fam if isinstance(fam, str) else None
        n_tools = int(snap.get("n_allowed_tools", 0))  # type: ignore[call-overload]
    return DecisionOutcomeRecord(
        decision_id=m["decision_id"],
        run_id=m["run_id"],
        tenant_id=m["tenant_id"],
        policy_version=m["policy_version"],
        acted=m["acted"],
        selection_probability=m["selection_probability"],
        family=family,
        risk_level=risk,
        action=key,
        status=OutcomeStatus(m["status"]),
        tokens=m["tokens_used"],
        n_allowed_tools=n_tools,
        actions=actions,
        default_key=default,
        propensities=propensities,
    )


class SqlDecisionLog:
    def __init__(self, engine: Engine):
        self._engine = engine

    def _select(self) -> sa.Select[tuple[object, ...]]:
        return sa.select(
            PD.c.decision_id,
            PD.c.run_id,
            PD.c.tenant_id,
            PD.c.policy_version,
            PD.c.feature_snapshot,
            PD.c.eligible_actions,
            PD.c.estimates,
            PD.c.selected_action,
            PD.c.selection_probability,
            PD.c.reason,
            PD.c.outcome_status,
            PD.c.acted,
            RO.c.status,
            RO.c.tokens_used,
        ).select_from(
            PD.join(RO, sa.and_(PD.c.run_id == RO.c.run_id, PD.c.tenant_id == RO.c.tenant_id))
        )

    def outcome_records(
        self,
        tenant_id: str,
        *,
        policy_versions: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[DecisionOutcomeRecord]:
        q = self._select().where(PD.c.tenant_id == tenant_id)
        if policy_versions:
            q = q.where(PD.c.policy_version.in_(list(policy_versions)))
        if since is not None:
            q = q.where(PD.c.created_at >= since)
        q = q.order_by(PD.c.decision_id)  # UUIDv7: time ordered
        if limit is not None:
            q = q.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [_record_of(r) for r in rows]

    def shadow_pairs(
        self, tenant_id: str, shadow_policy_version: str
    ) -> list[tuple[DecisionOutcomeRecord, ControllerDecision]]:
        q = (
            self._select()
            .where(
                PD.c.tenant_id == tenant_id,
                PD.c.policy_version == shadow_policy_version,
                PD.c.acted.is_(False),
            )
            .order_by(PD.c.decision_id)
        )
        with self._engine.connect() as conn:
            shadow_rows = conn.execute(q).mappings().all()
            run_ids = [r["run_id"] for r in shadow_rows]
            acted_rows = (
                conn.execute(
                    self._select().where(
                        PD.c.tenant_id == tenant_id,
                        PD.c.acted.is_(True),
                        PD.c.run_id.in_(run_ids),
                    )
                )
                .mappings()
                .all()
                if run_ids
                else []
            )
        by_run = {r["run_id"]: r for r in acted_rows}
        return [
            (_record_of(by_run[r["run_id"]]), _decision_from_row(r))
            for r in shadow_rows
            if r["run_id"] in by_run
        ]

    def decisions(
        self,
        tenant_id: str,
        policy_version: str,
        *,
        acted: bool | None = None,
        limit: int | None = None,
    ) -> list[ControllerDecision]:
        q = sa.select(PD).where(PD.c.tenant_id == tenant_id, PD.c.policy_version == policy_version)
        if acted is not None:
            q = q.where(PD.c.acted.is_(acted))
        q = q.order_by(PD.c.decision_id)
        if limit is not None:
            q = q.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [_decision_from_row(r) for r in rows]

    def count_trace_events(self, tenant_id: str, component: str, status: str) -> int:
        te = t.trace_events
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(te)
                    .where(
                        te.c.tenant_id == tenant_id,
                        te.c.component == component,
                        te.c.status == status,
                    )
                ).scalar_one()
            )
