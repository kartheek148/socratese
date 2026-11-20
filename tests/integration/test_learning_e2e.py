"""M3 integration tests against real PostgreSQL: trace -> case -> candidate, end to end."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.capability_store import SqlCapabilityStore
from experience_engine.adapters.db.experience_store import SqlExperienceStore
from experience_engine.adapters.db.job_queue import SqlLearningJobQueue
from experience_engine.adapters.db.learning_store import SqlLearningStore
from experience_engine.adapters.db.reader import SqlRunReader
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.adapters.llm.fake import HeuristicFakeLLM
from experience_engine.adapters.results import InMemoryResultStore
from experience_engine.bench.dataset import DEFAULT_SEED, BenchTask, generate_split
from experience_engine.bench.harness import run_benchmark
from experience_engine.bench.verifier import SimulationVerifier
from experience_engine.domain.benchmark import DatasetVersion, Split
from experience_engine.domain.capability import CapabilityState, RegistrationKind
from experience_engine.domain.experience import PIPELINE_VERSION, ExclusionReason
from experience_engine.execution.controller import FixedPolicy
from experience_engine.learn_cli import EvaluatorAccessError, assert_no_evaluator_access
from experience_engine.learning.collector import ExecutionCompletedHandler
from experience_engine.learning.compiler import CompilerConfig
from experience_engine.learning.pipeline import (
    CandidateCompilerService,
    ExperiencePipeline,
    LearningJobProcessor,
)
from support import FakeClock, config

pytestmark = pytest.mark.integration


class Env:
    def __init__(self, engine: Engine, tmp_path: Path) -> None:
        self.engine = engine
        self.artifacts = LocalFsArtifactStore(tmp_path / "artifacts")
        self.lifecycle = SqlRunLifecycle(engine)
        self.store = SqlExperienceStore(engine)
        self.caps = SqlCapabilityStore(engine)
        self.clock = FakeClock()
        self.handler = ExecutionCompletedHandler(SqlLearningStore(engine), self.clock)
        self.pipeline = ExperiencePipeline(
            reader=SqlRunReader(engine),
            artifacts=self.artifacts,
            store=self.store,
            clock=self.clock,
        )

    def persist(self, split: Split, tenant: str = "acme", seed: int = 0) -> list[str]:
        tasks = [
            BenchTask(
                task=bt.task.model_copy(update={"tenant_id": tenant}),
                environment=bt.environment,
                family=bt.family,
                variant=bt.variant,
            )
            for bt in generate_split(split, DEFAULT_SEED)
        ]
        records = run_benchmark(
            tasks,
            dataset=DatasetVersion(
                name="ds", version="1", split=split, content_hash="h" * 64, n_tasks=len(tasks)
            ),
            seed=seed,
            config=config(),
            llm_factory=lambda _t, _s: HeuristicFakeLLM(),
            controller=FixedPolicy(),
            verifier=SimulationVerifier(),
            clock=self.clock,
            store=InMemoryResultStore(),
            lifecycle=self.lifecycle,
            artifacts=self.artifacts,
        )
        return [r.run_id for r in records]

    def deliver_events(self) -> None:
        """Stand-in for relay + broker + consumer: feed outbox payloads to the handler."""
        with self.engine.connect() as conn:
            payloads = [r for r in conn.execute(sa.select(t.outbox_events.c.payload))]
        for (payload,) in payloads:
            self.handler.handle(payload)

    def process_jobs(self) -> None:
        proc = LearningJobProcessor(
            queue=SqlLearningJobQueue(self.engine), pipeline=self.pipeline, clock=self.clock
        )
        while proc.run_once().succeeded:
            pass

    def compiler(self, **cfg: object) -> CandidateCompilerService:
        return CandidateCompilerService(
            artifacts=self.artifacts,
            store=self.store,
            capabilities=self.caps,
            clock=self.clock,
            config=CompilerConfig(**cfg),  # type: ignore[arg-type]
        )


@pytest.fixture
def env(engine: Engine, tmp_path: Path) -> Env:
    return Env(engine, tmp_path)


def count(engine: Engine, table: sa.Table, *where: sa.ColumnElement[bool]) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(table).where(*where)).scalar_one()
        )


def test_trace_to_candidate_end_to_end_with_provenance_and_cost(env: Env) -> None:
    runs = env.persist(Split.DISCOVERY)
    env.deliver_events()
    env.process_jobs()

    assert count(env.engine, t.experience_cases) == 12
    assert count(env.engine, t.experience_cases, t.experience_cases.c.eligible.is_(True)) == 12
    assert count(env.engine, t.learning_jobs, t.learning_jobs.c.state == "SUCCEEDED") == 12
    # learning cost is accounted for (deterministic pipeline: wall time only, zero tokens)
    assert count(env.engine, t.cost_ledger, t.cost_ledger.c.pipeline == PIPELINE_VERSION) == 12

    summary = env.compiler().compile("acme")
    assert summary.n_eligible_cases == 12 and not summary.rejections
    assert sorted(r.kind for r in summary.registered) == [RegistrationKind.NEW_CAPABILITY] * 2

    versions = env.caps.list_versions("acme")
    assert len(versions) == 2 and {s for _, s in versions} == {CapabilityState.CANDIDATE}
    for definition, _ in versions:
        assert set(definition.provenance.source_run_ids) <= set(runs)
        assert definition.provenance.compiler_version == "rule-compiler/2"
        assert (definition.provenance.dataset_name, definition.provenance.dataset_version) == (
            "ds",
            "1",
        )
    assert count(env.engine, t.capability_evidence) == 12  # 6 verified source runs per family
    with env.engine.connect() as conn:
        tests = conn.execute(sa.select(t.capability_versions.c.tests)).scalars().all()
    assert all(any(x["kind"] == "SOURCE_REPLAY" for x in tt) for tt in tests)
    assert count(env.engine, t.cost_ledger, t.cost_ledger.c.subject_type == "compile") == 1


def test_candidates_are_not_available_to_normal_execution(env: Env) -> None:
    env.persist(Split.DISCOVERY)
    env.deliver_events()
    env.process_jobs()
    env.compiler().compile("acme")
    assert len(env.caps.list_versions("acme")) == 2
    assert env.caps.list_executable("acme") == []  # the catalog execution may use: ACTIVE only
    with env.engine.connect() as conn:
        states = conn.execute(sa.select(t.capability_versions.c.state)).scalars().all()
    assert set(states) == {"CANDIDATE"}


def test_reprocessing_is_idempotent_and_new_evidence_does_not_create_a_new_skill(env: Env) -> None:
    env.persist(Split.DISCOVERY)
    env.deliver_events()
    env.process_jobs()
    first = env.compiler().compile("acme")
    again = env.compiler().compile("acme")
    assert [r.kind for r in again.registered] == [RegistrationKind.NO_CHANGE] * 2
    assert count(env.engine, t.capability_versions) == 2

    env.deliver_events()  # redelivery: no new jobs/cases
    env.process_jobs()
    assert count(env.engine, t.experience_cases) == 12 and count(env.engine, t.learning_jobs) == 12

    env.persist(Split.DISCOVERY, seed=1)  # more runs of the same procedure
    env.deliver_events()
    env.process_jobs()
    more = env.compiler().compile("acme")
    assert [r.kind for r in more.registered] == [RegistrationKind.EVIDENCE_ADDED] * 2
    assert all(r.evidence_added == 6 for r in more.registered)
    assert count(env.engine, t.capabilities) == 2 and count(env.engine, t.capability_versions) == 2
    assert {r.version for r in first.registered + more.registered} == {1}


def test_changed_procedure_creates_new_immutable_version_keeping_the_old(env: Env) -> None:
    env.persist(Split.DISCOVERY)
    env.deliver_events()
    env.process_jobs()
    env.compiler().compile("acme")
    v2 = env.compiler(compiler_version="rule-compiler/3").compile("acme")
    assert [r.kind for r in v2.registered] == [RegistrationKind.NEW_VERSION] * 2
    assert sorted(r.version for r in v2.registered) == [2, 2]
    assert count(env.engine, t.capability_versions) == 4
    with env.engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                t.capability_versions.c.version,
                t.capability_versions.c.compiler_version,
                t.capability_versions.c.state,
            ).order_by(t.capability_versions.c.version)
        ).all()
    assert {(v, c) for v, c, _ in rows} == {(1, "rule-compiler/2"), (2, "rule-compiler/3")}
    assert {s for *_, s in rows} == {"CANDIDATE"}


def test_validation_and_hidden_runs_never_become_eligible_or_feed_the_compiler(env: Env) -> None:
    env.persist(Split.VALIDATION)
    env.persist(Split.HIDDEN_TRANSFER)
    env.deliver_events()
    env.process_jobs()
    assert count(env.engine, t.experience_cases) == 16
    assert count(env.engine, t.experience_cases, t.experience_cases.c.eligible.is_(True)) == 0
    with env.engine.connect() as conn:
        reasons = set(conn.execute(sa.select(t.experience_cases.c.excluded_reason)).scalars())
    assert reasons == {ExclusionReason.NON_DISCOVERY_SPLIT.value}
    summary = env.compiler(min_support=1).compile("acme")
    assert summary.n_eligible_cases == 0 and not summary.registered
    assert count(env.engine, t.capabilities) == 0


def test_database_refuses_eligible_non_discovery_cases(env: Env) -> None:
    (run_id, *_) = env.persist(Split.VALIDATION)
    base = dict(
        case_id="01a1f316-3845-794c-95cf-0c9e32b52350",
        tenant_id="acme",
        run_id=run_id,
        task_id="k",
        pipeline_version="x",
        task_family="pool",
        outcome_status="VERIFIED_SUCCESS",
        features={},
        n_steps=1,
        tokens_used=1,
        created_at=datetime.now(UTC),
    )
    for split, status in (
        ("VALIDATION", "VERIFIED_SUCCESS"),
        ("DISCOVERY", "UNKNOWN"),
        (None, "FAILURE"),
    ):
        with (
            pytest.raises(IntegrityError, match="eligible_requires_discovery"),
            env.engine.begin() as conn,
        ):
            conn.execute(
                t.experience_cases.insert().values(
                    **{**base, "dataset_split": split, "outcome_status": status, "eligible": True}
                )
            )
    with pytest.raises(IntegrityError, match="exclusion_has_reason"), env.engine.begin() as conn:
        conn.execute(
            t.experience_cases.insert().values(
                **{**base, "dataset_split": "VALIDATION", "eligible": False}
            )
        )


def test_untagged_runs_have_unknown_provenance_and_are_excluded(env: Env) -> None:
    env.persist(Split.DISCOVERY)
    with env.engine.begin() as conn:  # simulate a run recorded before provenance tags existed
        conn.execute(sa.text("UPDATE task_runs SET tags = '{}'::jsonb"))
    env.deliver_events()
    env.process_jobs()
    with env.engine.connect() as conn:
        reasons = set(conn.execute(sa.select(t.experience_cases.c.excluded_reason)).scalars())
    assert reasons == {ExclusionReason.UNKNOWN_PROVENANCE.value}
    assert env.compiler().compile("acme").n_eligible_cases == 0


def test_tenant_isolation_across_learning(env: Env) -> None:
    env.persist(Split.DISCOVERY, tenant="acme")
    env.deliver_events()
    env.process_jobs()
    env.compiler().compile("acme")
    assert env.compiler().compile("globex").n_eligible_cases == 0
    assert (
        env.caps.list_versions("globex") == []
        and env.store.list_eligible_cases("globex", PIPELINE_VERSION) == []
    )
    with env.engine.connect() as conn:
        cap = conn.execute(sa.select(t.capability_versions.c.capability_id)).scalars().first()
        run_id = conn.execute(sa.select(t.task_runs.c.run_id)).scalars().first()
    assert cap and run_id
    with (
        pytest.raises(IntegrityError),
        env.engine.begin() as conn,
    ):  # foreign tenant cannot attach evidence
        conn.execute(
            t.capability_evidence.insert().values(
                evidence_id="01a1f316-3845-794c-95cf-0c9e32b52351",
                tenant_id="globex",
                capability_id=cap,
                version=1,
                run_id=run_id,
                role="SOURCE_SUCCESS",
                created_at=datetime.now(UTC),
            )
        )
    assert SqlRunReader(env.engine).reconstruct("globex", run_id) is None


def test_immutability_triggers_on_learning_tables(env: Env) -> None:
    env.persist(Split.DISCOVERY)
    env.deliver_events()
    env.process_jobs()
    env.compiler().compile("acme")
    for stmt in (
        "UPDATE capability_versions SET definition = '{}'::jsonb",
        "UPDATE capability_versions SET compiler_version = 'forged'",
        "UPDATE capability_versions SET tests = '[]'::jsonb",
        "DELETE FROM capability_versions",
        "UPDATE experience_cases SET eligible = true",
        "DELETE FROM experience_cases",
        "UPDATE capability_evidence SET role = 'COUNTEREXAMPLE'",
        "DELETE FROM capability_evidence",
        "UPDATE cost_ledger SET wall_ms = 0",
        "DELETE FROM cost_ledger",
    ):
        with (
            pytest.raises(DBAPIError, match="append-only|immutable|cannot be deleted"),
            env.engine.begin() as conn,
        ):
            conn.execute(sa.text(stmt))
    with env.engine.begin() as conn:  # state is the only mutable column (registry, milestone 4)
        conn.execute(
            sa.text("UPDATE capability_versions SET state = 'VALIDATING' WHERE version = 1")
        )
    assert env.caps.list_executable("acme") == []


def test_job_lease_allows_recovery_after_a_worker_crash(env: Env) -> None:
    env.persist(Split.DISCOVERY)
    env.deliver_events()
    queue = SqlLearningJobQueue(env.engine, lease=timedelta(minutes=5))
    now = datetime.now(UTC)
    first = queue.claim_pending("trajectory_build", 4, now)
    assert len(first) == 4 and all(c.attempts == 1 for c in first)
    # the worker "crashes": nothing is marked. Within the lease the jobs are not handed out again.
    assert {
        c.job.job_id
        for c in queue.claim_pending("trajectory_build", 100, now + timedelta(minutes=1))
    }.isdisjoint({c.job.job_id for c in first})
    later = queue.claim_pending("trajectory_build", 100, now + timedelta(minutes=6))
    reclaimed = {c.job.job_id: c.attempts for c in later}
    assert all(reclaimed[c.job.job_id] == 2 for c in first)


def test_concurrent_workers_claim_disjoint_jobs(env: Env) -> None:
    import threading

    env.persist(Split.DISCOVERY)
    env.deliver_events()
    results: list[list[str]] = []
    barrier = threading.Barrier(3)

    def worker() -> None:
        q = SqlLearningJobQueue(env.engine)
        barrier.wait()
        results.append(
            [c.job.job_id for c in q.claim_pending("trajectory_build", 4, datetime.now(UTC))]
        )

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    flat = [j for r in results for j in r]
    assert len(flat) == len(set(flat)) == 12


def test_learning_plane_refuses_to_run_with_evaluator_access() -> None:
    assert_no_evaluator_access({})
    with pytest.raises(EvaluatorAccessError):
        assert_no_evaluator_access({"EE_EVALUATOR_ROLE": "evaluator"})
