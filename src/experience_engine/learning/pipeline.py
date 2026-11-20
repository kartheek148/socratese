"""Learning-plane orchestration (pure; all I/O behind ports).

* `ExperiencePipeline.process_run` - idempotently turns one completed run into an ExperienceCase
  (+ a normalized trajectory artifact). Only eligible cases can feed the compiler.
* `CandidateCompilerService.compile` - compiles registered candidates from eligible cases.
* `LearningJobProcessor` - claims `learning_jobs` and runs the pipeline with bounded retries.

None of this is on the execution path; execution never waits for it.
"""

import dataclasses
import logging
from dataclasses import dataclass

from experience_engine.domain.capability import RegistrationKind
from experience_engine.domain.experience import (
    PIPELINE_VERSION,
    ExperienceCase,
    Trajectory,
    classify_eligibility,
)
from experience_engine.domain.ids import new_id
from experience_engine.domain.retry import RetryPolicy
from experience_engine.domain.run import TRAJECTORY_JOB
from experience_engine.learning.compiler import (
    CompilerConfig,
    Rejection,
    compile_candidates,
)
from experience_engine.learning.normalizer import build_trajectory
from experience_engine.ports.artifacts import ArtifactStore
from experience_engine.ports.clock import Clock
from experience_engine.ports.experience import (
    CapabilityStore,
    ExperienceStore,
    LearningJobQueue,
    RegistrationResult,
)
from experience_engine.ports.persistence import RunReader

log = logging.getLogger("experience_engine.learning")


class RunNotFoundError(Exception):
    pass


class ExperiencePipeline:
    def __init__(
        self,
        *,
        reader: RunReader,
        artifacts: ArtifactStore,
        store: ExperienceStore,
        clock: Clock,
    ):
        self._reader = reader
        self._artifacts = artifacts
        self._store = store
        self._clock = clock

    def process_run(self, tenant_id: str, run_id: str) -> tuple[ExperienceCase, bool]:
        """Returns (case, created). Re-processing the same run is a no-op."""
        existing = self._store.get_case(tenant_id, run_id, PIPELINE_VERSION)
        if existing is not None:
            return existing, False
        t0 = self._clock.monotonic_ms()
        rec = self._reader.reconstruct(tenant_id, run_id)
        if rec is None:
            raise RunNotFoundError(f"run {run_id} not found for tenant")
        split = rec.run.tags.get("dataset_split")
        completed = rec.outcome is not None and rec.run.status.value == "COMPLETED"
        if rec.outcome is None:
            raise RunNotFoundError("run has no outcome yet")
        eligible, reason = classify_eligibility(
            rec.outcome.status,
            split,
            completed,
            capability_assisted=rec.outcome.capability_version is not None,
        )

        trajectory: Trajectory | None = None
        sha: str | None = None
        try:
            trajectory = build_trajectory(rec, self._artifacts.get)
        except Exception as exc:  # unreadable trace/artifact: record as excluded, never guess
            log.warning(
                "trajectory build failed", extra={"correlation_id": run_id, "error": str(exc)}
            )
            eligible, reason = False, classify_eligibility(rec.outcome.status, split, False)[1]
        if trajectory is not None:
            stored = self._artifacts.put(
                tenant_id,
                trajectory.model_dump_json().encode(),
                role="trajectory",
                content_type="application/json",
            )
            self._store.register_artifact(tenant_id, stored.ref, stored.uri)
            sha = stored.ref.sha256

        case = ExperienceCase(
            case_id=new_id(),
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=rec.run.task_id,
            task_family=rec.task.task_family,
            outcome_status=rec.outcome.status,
            dataset_split=split,
            eligible=eligible,
            excluded_reason=reason,
            features={
                "risk_level": rec.task.risk_level,
                "file_roles": trajectory.file_roles if trajectory else [],
                "config_shape": trajectory.config_shape if trajectory else None,
                "config_id": rec.run.config_id,
                "policy_version": rec.run.policy_version,
                "dataset_name": rec.run.tags.get("dataset_name"),
                "dataset_version": rec.run.tags.get("dataset_version"),
            },
            trajectory_sha256=sha,
            n_steps=len(trajectory.steps) if trajectory else 0,
            tokens_used=rec.outcome.tokens_used,
            created_at=self._clock.now(),
        )
        created = self._store.save_case(case)
        self._store.record_cost(
            tenant_id=tenant_id,
            pipeline=PIPELINE_VERSION,
            subject_type="run",
            subject_id=run_id,
            tokens=0,
            tool_calls=0,
            wall_ms=max(0, self._clock.monotonic_ms() - t0),
            correlation_id=rec.run.correlation_id,
            now=self._clock.now(),
        )
        if not created:  # lost a race with a concurrent worker
            stored_case = self._store.get_case(tenant_id, run_id, PIPELINE_VERSION)
            assert stored_case is not None
            return stored_case, False
        return case, True


@dataclass(frozen=True)
class CompileSummary:
    registered: list[RegistrationResult]
    rejections: list[Rejection]
    n_eligible_cases: int


class CandidateCompilerService:
    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        store: ExperienceStore,
        capabilities: CapabilityStore,
        clock: Clock,
        config: CompilerConfig | None = None,
    ):
        self._artifacts = artifacts
        self._store = store
        self._capabilities = capabilities
        self._clock = clock
        self._config = config or CompilerConfig()

    def _dataset_config(self, cases: list[ExperienceCase]) -> CompilerConfig:
        """Record dataset provenance when every contributing case came from one dataset."""
        pairs = {(c.features.get("dataset_name"), c.features.get("dataset_version")) for c in cases}
        if len(pairs) == 1:
            name, version = next(iter(pairs))
            if isinstance(name, str) and isinstance(version, str):
                return dataclasses.replace(self._config, dataset_name=name, dataset_version=version)
        return self._config

    def compile(self, tenant_id: str) -> CompileSummary:
        t0 = self._clock.monotonic_ms()
        cases = self._store.list_eligible_cases(tenant_id, PIPELINE_VERSION)
        trajectories: list[Trajectory] = []
        for case in cases:
            if case.trajectory_sha256 is None:
                continue
            raw = self._artifacts.get(tenant_id, case.trajectory_sha256)
            trajectories.append(Trajectory.model_validate_json(raw))
        result = compile_candidates(trajectories, self._dataset_config(cases))
        registered: list[RegistrationResult] = []
        for bundle in result.candidates:
            defn = bundle.definition
            reg = self._capabilities.register_candidate(
                tenant_id,
                bundle,
                list(defn.provenance.source_run_ids),
                list(defn.provenance.counterexample_run_ids),
                self._clock.now(),
            )
            registered.append(reg)
            log.info(
                "candidate registered",
                extra={"capability_id": reg.capability_id, "kind": reg.kind.value},
            )
        self._store.record_cost(
            tenant_id=tenant_id,
            pipeline=self._config.compiler_version,
            subject_type="compile",
            subject_id=f"{len(cases)} cases",
            tokens=0,
            tool_calls=0,
            wall_ms=max(0, self._clock.monotonic_ms() - t0),
            correlation_id=new_id(),
            now=self._clock.now(),
        )
        return CompileSummary(
            registered=registered, rejections=result.rejections, n_eligible_cases=len(cases)
        )


@dataclass(frozen=True)
class JobBatchResult:
    succeeded: int = 0
    retried: int = 0
    failed: int = 0


class LearningJobProcessor:
    """Bounded retries; a crashed worker's RUNNING job is re-claimed after its lease expires."""

    def __init__(
        self,
        *,
        queue: LearningJobQueue,
        pipeline: ExperiencePipeline,
        clock: Clock,
        retry: RetryPolicy | None = None,
    ):
        self._queue = queue
        self._pipeline = pipeline
        self._clock = clock
        self._retry = retry or RetryPolicy(max_attempts=3)

    def run_once(self, batch: int = 20) -> JobBatchResult:
        ok = retried = failed = 0
        for claimed in self._queue.claim_pending(TRAJECTORY_JOB, batch, self._clock.now()):
            job = claimed.job
            try:
                self._pipeline.process_run(job.tenant_id, job.run_id)
            except Exception as exc:
                give_up = self._retry.exhausted(claimed.attempts) or isinstance(
                    exc, RunNotFoundError
                )
                self._queue.mark_retry_or_failed(
                    job.tenant_id,
                    job.job_id,
                    f"{type(exc).__name__}: {exc}"[:500],
                    give_up,
                    self._clock.now(),
                )
                failed += int(give_up)
                retried += int(not give_up)
                continue
            self._queue.mark_succeeded(job.tenant_id, job.job_id, self._clock.now())
            ok += 1
        return JobBatchResult(succeeded=ok, retried=retried, failed=failed)


__all__ = [
    "CandidateCompilerService",
    "CompileSummary",
    "ExperiencePipeline",
    "JobBatchResult",
    "LearningJobProcessor",
    "RegistrationKind",
    "RunNotFoundError",
]
