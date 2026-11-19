"""End-to-end: persisted run -> outbox relay -> RabbitMQ -> Celery worker -> idempotent job."""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from celery import Celery
from celery.contrib.testing.worker import start_worker
from kombu import Connection
from sqlalchemy import Engine

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.clock import SystemClock
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.engine import InfraSettings
from experience_engine.adapters.db.learning_store import SqlLearningStore
from experience_engine.adapters.db.outbox import SqlOutbox
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.adapters.mq.celery_app import (
    TASK_NAME,
    CeleryEventPublisher,
    create_celery_app,
    ensure_topology,
)
from experience_engine.domain.retry import RetryPolicy
from experience_engine.domain.run import OutboxEvent
from experience_engine.learning.collector import ExecutionCompletedHandler
from experience_engine.ports.learning import EnqueueResult
from support import correct_pool_fix, first_task, run_persisted

pytestmark = pytest.mark.integration
FAST_RETRY = RetryPolicy(max_attempts=3, base_delay_s=0.01, max_delay_s=0.05)


def wait_for(cond: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def count(engine: Engine, table: sa.Table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def dlq_messages(infra: InfraSettings) -> list[str]:
    with Connection(infra.amqp_url) as conn:
        queue = conn.SimpleQueue(infra.dead_letter_queue)
        out = []
        while True:
            try:
                msg = queue.get(block=False)
            except queue.Empty:
                break
            out.append(msg.headers.get("id", ""))
            msg.ack()
        queue.close()
        return out


def queue_depth(infra: InfraSettings, name: str) -> int:
    with Connection(infra.amqp_url) as conn:
        return int(conn.default_channel.queue_declare(name, passive=True).message_count)


@contextmanager
def running_worker(app: Celery, infra: InfraSettings) -> Iterator[None]:
    ensure_topology(app, infra)
    with start_worker(
        app,
        pool="solo",
        concurrency=1,
        perform_ping_check=False,
        loglevel="WARNING",
        queues=[infra.learning_queue],
        shutdown_timeout=15,
    ):
        yield


@pytest.fixture
def handler_and_store(engine: Engine) -> tuple[ExecutionCompletedHandler, SqlLearningStore]:
    store = SqlLearningStore(engine)
    return ExecutionCompletedHandler(store, SystemClock()), store


def _persisted_run(engine: Engine, tmp_path: Path, replies: list[object] | None = None) -> str:
    task = first_task()
    return run_persisted(
        task,
        correct_pool_fix(task) if replies is None else replies,
        SqlRunLifecycle(engine),
        LocalFsArtifactStore(tmp_path / "artifacts"),
        tenant="acme",
    ).run_id


def test_end_to_end_run_to_learning_job_and_redelivery_is_idempotent(
    engine: Engine,
    infra: InfraSettings,
    handler_and_store,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    handler, _store = handler_and_store
    app = create_celery_app(infra, handler, FAST_RETRY)
    run_id = _persisted_run(engine, tmp_path)
    publisher = CeleryEventPublisher(app, infra)

    with running_worker(app, infra):
        assert SqlOutbox(engine).relay_once(publisher).published == 1
        wait_for(lambda: count(engine, t.learning_jobs) == 1)

        # Redelivery of the very same event, plus a reconciliation-style republish.
        with engine.connect() as conn:
            row = conn.execute(sa.select(t.outbox_events)).mappings().one()
        event = OutboxEvent(
            event_id=row["event_id"],
            tenant_id=row["tenant_id"],
            type=row["type"],
            aggregate_id=row["aggregate_id"],
            payload=row["payload"],
            correlation_id=row["correlation_id"],
            created_at=row["created_at"],
        )
        for _ in range(3):
            publisher.publish(event)
        wait_for(lambda: queue_depth(infra, infra.learning_queue) == 0)
        time.sleep(1.0)

    assert count(engine, t.learning_jobs) == 1
    assert count(engine, t.consumed_events) == 1
    with engine.connect() as conn:
        job = conn.execute(sa.select(t.learning_jobs)).mappings().one()
    assert job["run_id"] == run_id and job["tenant_id"] == "acme"
    assert (
        job["outcome_status"] == "VERIFIED_SUCCESS" and job["eligible_for_success_mining"] is True
    )
    assert job["correlation_id"] == run_id and job["dedupe_key"].startswith(run_id)
    assert dlq_messages(infra) == []


def test_unverified_outcomes_are_enqueued_but_not_eligible_for_mining(
    engine: Engine,
    infra: InfraSettings,
    handler_and_store,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    handler, _ = handler_and_store
    app = create_celery_app(infra, handler, FAST_RETRY)
    _persisted_run(engine, tmp_path)  # verified success
    _persisted_run(engine, tmp_path, [{"final": "I fixed it, trust me"}])  # false claim -> FAILURE
    with running_worker(app, infra):
        SqlOutbox(engine).relay_once(CeleryEventPublisher(app, infra))
        wait_for(lambda: count(engine, t.learning_jobs) == 2)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                t.learning_jobs.c.outcome_status, t.learning_jobs.c.eligible_for_success_mining
            )
        ).all()
    assert sorted(rows) == [("FAILURE", False), ("VERIFIED_SUCCESS", True)]


def test_poison_message_is_dead_lettered_not_retried_forever(
    engine: Engine,
    infra: InfraSettings,
    handler_and_store,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    handler, _ = handler_and_store
    app = create_celery_app(infra, handler, FAST_RETRY)
    with running_worker(app, infra):
        app.send_task(
            TASK_NAME,
            args=[{"event_id": "not-a-uuid", "tenant_id": "acme"}],
            queue=infra.learning_queue,
            task_id="poison-1",
        )
        wait_for(lambda: count(engine, t.dead_letters) == 1)
        wait_for(lambda: queue_depth(infra, infra.dead_letter_queue) == 1)
    with engine.connect() as conn:
        dead = conn.execute(sa.select(t.dead_letters)).mappings().one()
    assert dead["tenant_id"] == "acme" and dead["attempts"] == 1 and "invalid" in dead["reason"]
    assert queue_depth(infra, infra.learning_queue) == 0
    assert count(engine, t.learning_jobs) == 0


class FlakyStore(SqlLearningStore):
    def __init__(self, engine: Engine, failures: int) -> None:
        super().__init__(engine)
        self.failures = failures
        self.calls = 0

    def register_and_enqueue(self, consumer, event, job):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("database temporarily unavailable")
        return super().register_and_enqueue(consumer, event, job)


def test_transient_failures_are_retried_with_bounded_attempts_then_succeed(
    engine: Engine, infra: InfraSettings, tmp_path: Path
) -> None:
    store = FlakyStore(engine, failures=2)
    app = create_celery_app(infra, ExecutionCompletedHandler(store, SystemClock()), FAST_RETRY)
    _persisted_run(engine, tmp_path)
    with running_worker(app, infra):
        SqlOutbox(engine).relay_once(CeleryEventPublisher(app, infra))
        wait_for(lambda: count(engine, t.learning_jobs) == 1)
    assert store.calls == 3  # two transient failures + one success, within max_attempts=3
    assert count(engine, t.dead_letters) == 0


def test_exhausted_retries_end_in_the_dead_letter_queue_with_a_record(
    engine: Engine, infra: InfraSettings, tmp_path: Path
) -> None:
    store = FlakyStore(engine, failures=10**6)
    app = create_celery_app(infra, ExecutionCompletedHandler(store, SystemClock()), FAST_RETRY)
    _persisted_run(engine, tmp_path)
    with running_worker(app, infra):
        SqlOutbox(engine).relay_once(CeleryEventPublisher(app, infra))
        wait_for(lambda: count(engine, t.dead_letters) == 1)
        wait_for(lambda: queue_depth(infra, infra.dead_letter_queue) == 1)
    assert store.calls == FAST_RETRY.max_attempts
    with engine.connect() as conn:
        dead = conn.execute(sa.select(t.dead_letters)).mappings().one()
    assert dead["attempts"] == FAST_RETRY.max_attempts and "ConnectionError" in dead["reason"]
    assert count(engine, t.learning_jobs) == 0 and queue_depth(infra, infra.learning_queue) == 0


def test_message_survives_until_a_worker_exists(
    engine: Engine,
    infra: InfraSettings,
    handler_and_store,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    """Outbox -> broker works with no consumer running; the durable queue holds the message."""
    handler, _ = handler_and_store
    app = create_celery_app(infra, handler, FAST_RETRY)
    _persisted_run(engine, tmp_path)
    assert SqlOutbox(engine).relay_once(CeleryEventPublisher(app, infra)).published == 1
    assert queue_depth(infra, infra.learning_queue) == 1 and count(engine, t.learning_jobs) == 0
    with running_worker(app, infra):
        wait_for(lambda: count(engine, t.learning_jobs) == 1)


def test_publish_failure_is_reported_so_the_outbox_keeps_the_row(
    engine: Engine,
    infra: InfraSettings,
    handler_and_store,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    handler, _ = handler_and_store
    broken = InfraSettings(
        database_url=infra.database_url,
        amqp_url="amqp://ee:wrong@127.0.0.1:1//",
        artifact_root=infra.artifact_root,
        learning_queue=infra.learning_queue,
        dead_letter_queue=infra.dead_letter_queue,
    )
    app = create_celery_app(broken, handler, FAST_RETRY)
    _persisted_run(engine, tmp_path)
    res = SqlOutbox(engine).relay_once(CeleryEventPublisher(app, broken))
    assert (res.published, res.failed) == (0, 1)
    assert SqlOutbox(engine).stats()["PENDING"] == 1
    assert EnqueueResult.ENQUEUED.value == "ENQUEUED"
