"""Controller policy registry: the only component that moves a policy version between states.

A forward transition needs a stored PASS evaluation of the previous stage for exactly this
definition hash and the current gate set. Rollback restores the previously active version in the
same transaction. Rejected and rolled-back versions are terminal; a re-admission is a new version.
"""

from experience_engine.domain.controller import ControllerPolicyDefinition
from experience_engine.domain.evaluation import Verdict
from experience_engine.domain.rollout import (
    REQUIRED_STAGE,
    PolicyState,
    RolloutGates,
    StageReport,
    can_transition,
)
from experience_engine.ports.clock import Clock
from experience_engine.ports.controller_store import (
    ControllerPolicyStore,
    PolicyRecord,
    PolicyTransition,
)

S = PolicyState


class ControllerRegistryError(Exception):
    pass


class IllegalPolicyTransitionError(ControllerRegistryError):
    pass


class PromotionBlockedError(ControllerRegistryError):
    """A promotion gate is not satisfied. The message lists every unmet requirement."""


class ControllerPolicyRegistry:
    def __init__(
        self, store: ControllerPolicyStore, clock: Clock, gates: RolloutGates | None = None
    ):
        self._store = store
        self._clock = clock
        self.gates = gates or RolloutGates()

    # ---- helpers ------------------------------------------------------------------------------
    def _get(self, tenant_id: str, policy_id: str, version: int) -> PolicyRecord:
        rec = self._store.get_version(tenant_id, policy_id, version)
        if rec is None:
            raise ControllerRegistryError(f"{policy_id}/{version} not found for tenant")
        return rec

    def _in_state(self, tenant_id: str, policy_id: str, state: PolicyState) -> PolicyRecord | None:
        found = [r for r in self._store.list_versions(tenant_id, policy_id) if r.state == state]
        return found[0] if found else None

    # ---- lifecycle ----------------------------------------------------------------------------
    def register(self, definition: ControllerPolicyDefinition) -> PolicyRecord:
        """Add an immutable CANDIDATE. The version must be the next free number (no overwrites)."""
        existing = self._store.list_versions(definition.tenant_id, definition.policy_id)
        expected = max((r.version for r in existing), default=0) + 1
        if definition.version != expected:
            raise ControllerRegistryError(
                f"{definition.policy_id} version must be {expected} "
                "(versions are immutable and sequential)"
            )
        return self._store.add_version(definition, self._clock.now())

    def record_evaluation(self, tenant_id: str, report: StageReport) -> str:
        policy_id, _, v = report.policy_version.rpartition("/")
        rec = self._get(tenant_id, policy_id, int(v))
        if report.definition_hash != rec.definition_hash:
            raise ControllerRegistryError("evaluation was computed for a different definition")
        return self._store.add_evaluation(
            tenant_id, policy_id, rec.version, report, self._clock.now()
        )

    def promote(
        self, tenant_id: str, policy_id: str, version: int, to: PolicyState, actor: str, reason: str
    ) -> None:
        rec = self._get(tenant_id, policy_id, version)
        if to not in REQUIRED_STAGE:
            raise IllegalPolicyTransitionError(f"cannot promote to {to.value}")
        if not can_transition(rec.state, to):
            raise IllegalPolicyTransitionError(f"{rec.state.value} -> {to.value} is not allowed")
        stage = REQUIRED_STAGE[to]
        problems: list[str] = []
        ev = self._store.latest_evaluation(tenant_id, policy_id, version, stage)
        if ev is None:
            problems.append(f"no {stage.value} evaluation recorded")
        else:
            r = ev.report
            if r.verdict != Verdict.PASS:
                problems.append(
                    f"latest {stage.value} verdict is {r.verdict.value}: {'; '.join(r.reasons)}"
                )
            if r.definition_hash != rec.definition_hash:
                problems.append(f"{stage.value} evidence is for a different definition")
            if r.gate_hash != self.gates.gate_hash(stage):
                problems.append(
                    f"{stage.value} evidence used a different gate set than the current one"
                )
        transitions = [PolicyTransition(version, rec.state, to)]
        active = self._in_state(tenant_id, policy_id, S.ACTIVE)
        if to == S.ACTIVE and ev is not None:
            started = max(
                (
                    h.created_at
                    for h in self._store.history(tenant_id, policy_id)
                    if h.version == version and h.to_state == S.CANARY
                ),
                default=None,
            )
            if started is None or ev.created_at < started:
                problems.append("canary evidence predates the canary period (not concurrent)")
            control = ev.report.metrics.get("control_version")
            expected = active.definition.policy_version if active else None
            if control != expected:
                problems.append(
                    f"canary control was {control!r} but the active version is {expected!r}"
                )
            if active is not None:
                transitions.insert(0, PolicyTransition(active.version, S.ACTIVE, S.SUPERSEDED))
        if to == S.SHADOW and self._in_state(tenant_id, policy_id, S.SHADOW) is not None:
            problems.append("another version is already in SHADOW")
        if to == S.CANARY and self._in_state(tenant_id, policy_id, S.CANARY) is not None:
            problems.append("another version is already in CANARY")
        if problems:
            raise PromotionBlockedError(
                f"{policy_id}/{version} -> {to.value} blocked: " + " | ".join(problems)
            )
        self._store.apply_transitions(
            tenant_id,
            policy_id,
            transitions,
            actor,
            reason[:500],
            ev.evaluation_id if ev else None,
            self._clock.now(),
        )

    def reject(self, tenant_id: str, policy_id: str, version: int, actor: str, reason: str) -> None:
        rec = self._get(tenant_id, policy_id, version)
        if not can_transition(rec.state, S.REJECTED):
            raise IllegalPolicyTransitionError(f"{rec.state.value} -> REJECTED is not allowed")
        self._store.apply_transitions(
            tenant_id,
            policy_id,
            [PolicyTransition(version, rec.state, S.REJECTED)],
            actor,
            reason[:500],
            None,
            self._clock.now(),
        )

    def rollback(
        self, tenant_id: str, policy_id: str, version: int, actor: str, reason: str
    ) -> None:
        """SHADOW/CANARY/ACTIVE -> ROLLED_BACK. Rolling back ACTIVE restores the most recently
        superseded version (if any) in the same transaction; otherwise the rule baseline serves."""
        rec = self._get(tenant_id, policy_id, version)
        if not can_transition(rec.state, S.ROLLED_BACK):
            raise IllegalPolicyTransitionError(f"{rec.state.value} -> ROLLED_BACK is not allowed")
        transitions = [PolicyTransition(version, rec.state, S.ROLLED_BACK)]
        if rec.state == S.ACTIVE:
            superseded = [
                r
                for r in self._store.list_versions(tenant_id, policy_id)
                if r.state == S.SUPERSEDED and r.version < version
            ]
            if superseded:
                prev = max(superseded, key=lambda r: r.version)
                transitions.append(PolicyTransition(prev.version, S.SUPERSEDED, S.ACTIVE))
        self._store.apply_transitions(
            tenant_id, policy_id, transitions, actor, reason[:500], None, self._clock.now()
        )

    def apply_canary_guard(
        self, tenant_id: str, policy_id: str, version: int, report: StageReport
    ) -> bool:
        """Record canary evidence; a FAIL rolls the canary back. True if it was rolled back."""
        self.record_evaluation(tenant_id, report)
        if report.verdict == Verdict.FAIL:
            self.rollback(tenant_id, policy_id, version, "canary-guard", "; ".join(report.reasons))
            return True
        return False
