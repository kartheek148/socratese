from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from experience_engine.domain.capability import CapabilityState
from experience_engine.domain.evaluation import EvaluationReport, Verdict
from experience_engine.domain.trace import ArtifactRef


class StaleStateError(Exception):
    """The version is no longer in the state the caller expected (concurrent change)."""


@dataclass(frozen=True)
class VersionRecord[D]:
    """One immutable version of a governed asset. `capability_id` holds the asset id (a capability
    id or a memory id): the name is kept so capability call sites are untouched."""

    tenant_id: str
    capability_id: str
    version: int
    state: CapabilityState
    definition: D


@dataclass(frozen=True)
class Pointer:
    active_version: int | None = None
    previous_version: int | None = None


@dataclass(frozen=True)
class TransitionSpec:
    version: int
    expected_from: CapabilityState
    to_state: CapabilityState


@dataclass(frozen=True)
class EvaluationSummary:
    evaluation_id: str
    verdict: Verdict
    policy_hash: str
    risk_class: str


@dataclass(frozen=True)
class ObservationStats:
    n: int
    capability_verified: int
    n_paired: int  # observations that also have a baseline result
    regressions: int  # baseline verified but capability did not


class RegistryStore[D](Protocol):
    def get_version(
        self, tenant_id: str, capability_id: str, version: int
    ) -> VersionRecord[D] | None: ...

    def get_pointer(self, tenant_id: str, capability_id: str) -> Pointer: ...

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
        """Atomically apply all transitions (each checked against its expected state), the
        optional pointer update, and one audit record per transition."""
        ...

    def record_evaluation(
        self, report: EvaluationReport, report_sha256: str, now: datetime
    ) -> str: ...

    def register_artifact(self, tenant_id: str, ref: ArtifactRef, uri: str) -> None: ...

    def latest_evaluation(
        self, tenant_id: str, capability_id: str, version: int
    ) -> EvaluationSummary | None: ...

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
    ) -> None: ...

    def observation_stats(
        self, tenant_id: str, capability_id: str, version: int, mode: str
    ) -> ObservationStats: ...
