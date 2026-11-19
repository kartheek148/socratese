from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from experience_engine.domain.capability import (
    CandidateBundle,
    CapabilityDefinition,
    CapabilityState,
    RegistrationKind,
)
from experience_engine.domain.experience import ExperienceCase
from experience_engine.domain.run import LearningJob
from experience_engine.domain.trace import ArtifactRef


@dataclass(frozen=True)
class ClaimedJob:
    job: LearningJob
    attempts: int


@dataclass(frozen=True)
class RegistrationResult:
    kind: RegistrationKind
    capability_id: str
    version: int
    evidence_added: int


class LearningJobQueue(Protocol):
    def claim_pending(self, job_type: str, batch: int, now: datetime) -> list[ClaimedJob]:
        """Atomically move PENDING (or lease-expired RUNNING) jobs to RUNNING, attempts += 1."""
        ...

    def mark_succeeded(self, tenant_id: str, job_id: str, now: datetime) -> None: ...

    def mark_retry_or_failed(
        self, tenant_id: str, job_id: str, error: str, give_up: bool, now: datetime
    ) -> None: ...


class ExperienceStore(Protocol):
    def get_case(
        self, tenant_id: str, run_id: str, pipeline_version: str
    ) -> ExperienceCase | None: ...

    def save_case(self, case: ExperienceCase) -> bool:
        """Insert-if-absent; returns False when a case for (run, pipeline_version) exists."""
        ...

    def register_artifact(self, tenant_id: str, ref: ArtifactRef, uri: str) -> None: ...

    def list_eligible_cases(
        self, tenant_id: str, pipeline_version: str
    ) -> list[ExperienceCase]: ...

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
    ) -> None: ...


class CapabilityStore(Protocol):
    def register_candidate(
        self,
        tenant_id: str,
        bundle: CandidateBundle,
        source_run_ids: list[str],
        counterexample_run_ids: list[str],
        now: datetime,
    ) -> RegistrationResult:
        """Create a new capability, add evidence to an unchanged procedure, or create a new
        immutable version of a changed one. Always as CANDIDATE."""
        ...

    def list_versions(
        self, tenant_id: str, states: frozenset[CapabilityState] | None = None
    ) -> list[tuple[CapabilityDefinition, CapabilityState]]: ...


class ExecutableCapabilityCatalog(Protocol):
    """The ONLY capability view normal task execution may use: ACTIVE versions, nothing else."""

    def list_executable(self, tenant_id: str) -> list[CapabilityDefinition]: ...
