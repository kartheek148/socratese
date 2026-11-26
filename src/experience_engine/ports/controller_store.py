"""Persistence port for controller policy versions, rollout evidence and logged decisions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from experience_engine.domain.controller import ControllerPolicyDefinition, DecisionOutcomeRecord
from experience_engine.domain.decision import ControllerDecision
from experience_engine.domain.rollout import PolicyState, Stage, StageReport


@dataclass(frozen=True)
class PolicyRecord:
    tenant_id: str
    policy_id: str
    version: int
    definition: ControllerPolicyDefinition
    definition_hash: str
    state: PolicyState
    created_at: datetime


@dataclass(frozen=True)
class StageEvidence:
    evaluation_id: str
    report: StageReport
    created_at: datetime


@dataclass(frozen=True)
class PolicyTransition:
    version: int
    from_state: PolicyState
    to_state: PolicyState


@dataclass(frozen=True)
class TransitionRecord:
    policy_id: str
    version: int
    from_state: PolicyState
    to_state: PolicyState
    actor: str
    reason: str
    evaluation_id: str | None
    created_at: datetime


class ControllerPolicyStore(Protocol):
    """Every method is tenant-scoped. Definitions are immutable; only `state` changes, and only
    through `apply_transitions` (atomic, audited)."""

    def add_version(
        self, definition: ControllerPolicyDefinition, now: datetime
    ) -> PolicyRecord: ...

    def get_version(self, tenant_id: str, policy_id: str, version: int) -> PolicyRecord | None: ...

    def list_versions(self, tenant_id: str, policy_id: str) -> list[PolicyRecord]: ...

    def add_evaluation(
        self, tenant_id: str, policy_id: str, version: int, report: StageReport, now: datetime
    ) -> str: ...

    def latest_evaluation(
        self, tenant_id: str, policy_id: str, version: int, stage: Stage
    ) -> StageEvidence | None: ...

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
        """All-or-nothing. Fails if a version is not in its stated `from_state`, or if the result
        would leave two ACTIVE / CANARY / SHADOW versions of one policy."""
        ...

    def history(self, tenant_id: str, policy_id: str) -> list[TransitionRecord]: ...


class DecisionLog(Protocol):
    """Read side of the decision log for the learning plane (never used by execution)."""

    def outcome_records(
        self,
        tenant_id: str,
        *,
        policy_versions: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[DecisionOutcomeRecord]:
        """Acted and shadow decisions joined with verified outcomes, oldest first."""
        ...

    def shadow_pairs(
        self, tenant_id: str, shadow_policy_version: str
    ) -> list[tuple[DecisionOutcomeRecord, ControllerDecision]]:
        """Runs where `shadow_policy_version` produced a non-acted decision, with the acted
        decision's verified outcome."""
        ...

    def decisions(
        self,
        tenant_id: str,
        policy_version: str,
        *,
        acted: bool | None = None,
        limit: int | None = None,
    ) -> list[ControllerDecision]:
        """Stored decisions of one policy version, oldest first (for replay checks)."""
        ...

    def count_trace_events(self, tenant_id: str, component: str, status: str) -> int: ...
