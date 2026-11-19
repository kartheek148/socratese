import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.artifacts.local_fs import LocalFsArtifactStore
from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.outbox import SqlOutbox
from experience_engine.adapters.db.run_lifecycle import SqlRunLifecycle
from experience_engine.domain.retry import RetryPolicy
from experience_engine.domain.run import OutboxEvent
from experience_engine.ports.events import PublishError
from support import correct_pool_fix, first_task, run_persisted

pytestmark = pytest.mark.integration


class RecordingPublisher:
    def __init__(self, fail_with: Exception | None = None, delay_s: float = 0.0) -> None:
        self.published: list[str] = []
        self.fail_with = fail_with
        self.delay_s = delay_s
        self._lock = threading.Lock()

    def publish(self, event: OutboxEvent) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        time.sleep(self.delay_s)
        with self._lock:
            self.published.append(event.event_id)


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _make_runs(engine: Engine, tmp_path: Path, n: int) -> list[str]:
    lifecycle = SqlRunLifecycle(engine)
    artifacts = LocalFsArtifactStore(tmp_path / "artifacts")
    task = first_task()
    return [
        run_persisted(task, correct_pool_fix(task), lifecycle, artifacts, tenant="acme").run_id
        for _ in range(n)
    ]


def _rows(engine: Engine) -> list[sa.RowMapping]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(t.outbox_events)).mappings())


def test_relay_publishes_once_and_marks_published(engine: Engine, tmp_path: Path) -> None:
    _make_runs(engine, tmp_path, 3)
    outbox, pub = SqlOutbox(engine), RecordingPublisher()
    assert outbox.relay_once(pub).published == 3
    assert outbox.relay_once(pub).published == 0
    assert len(pub.published) == len(set(pub.published)) == 3
    assert {r["status"] for r in _rows(engine)} == {"PUBLISHED"}
    assert all(r["published_at"] is not None for r in _rows(engine))
    assert outbox.stats() == {"PENDING": 0, "PUBLISHED": 3, "DEAD_LETTERED": 0}


def test_broker_outage_backs_off_without_burning_the_whole_queue(
    engine: Engine, tmp_path: Path
) -> None:
    _make_runs(engine, tmp_path, 3)
    clock = Clock()
    outbox = SqlOutbox(
        engine, RetryPolicy(max_attempts=5, base_delay_s=10, max_delay_s=100), clock, lambda: 1.0
    )
    down = RecordingPublisher(fail_with=PublishError("broker down"))
    res = outbox.relay_once(down)
    assert (res.published, res.failed) == (0, 1)  # stopped at the first failure
    attempts = sorted(r["attempts"] for r in _rows(engine))
    assert attempts == [0, 0, 1]
    assert outbox.relay_once(RecordingPublisher()).published == 2  # backed-off row is not yet due
    clock.advance(11)
    up = RecordingPublisher()
    assert outbox.relay_once(up).published == 1
    assert outbox.stats()["PENDING"] == 0


def test_exhausted_retries_dead_letter_with_audit_and_can_be_requeued(
    engine: Engine, tmp_path: Path
) -> None:
    _make_runs(engine, tmp_path, 1)
    clock = Clock()
    outbox = SqlOutbox(
        engine, RetryPolicy(max_attempts=3, base_delay_s=1, max_delay_s=1), clock, lambda: 0.0
    )
    down = RecordingPublisher(fail_with=PublishError("nope"))
    for _ in range(3):
        outbox.relay_once(down)
        clock.advance(5)
    (row,) = _rows(engine)
    assert row["status"] == "DEAD_LETTERED" and row["attempts"] == 3 and "nope" in row["last_error"]
    assert outbox.relay_once(RecordingPublisher()).published == 0  # dead rows are not retried
    with engine.connect() as conn:
        actions = conn.execute(sa.select(t.audit_log.c.action)).scalars().all()
    assert "outbox.dead_lettered" in actions
    assert not outbox.requeue_dead_letter("other-tenant", row["event_id"])
    assert outbox.requeue_dead_letter("acme", row["event_id"])
    assert outbox.relay_once(RecordingPublisher()).published == 1


def test_recovery_after_simulated_process_crash_is_at_least_once(
    engine: Engine, tmp_path: Path
) -> None:
    _make_runs(engine, tmp_path, 3)
    outbox = SqlOutbox(engine)

    class CrashingPublisher(RecordingPublisher):
        def publish(self, event: OutboxEvent) -> None:
            if len(self.published) == 1:
                raise RuntimeError("process killed")  # not a PublishError: simulates a crash
            super().publish(event)

    crashing = CrashingPublisher()
    with pytest.raises(RuntimeError):
        outbox.relay_once(crashing)
    # The transaction rolled back: nothing is marked published, nothing is lost.
    assert {r["status"] for r in _rows(engine)} == {"PENDING"}
    assert {r["attempts"] for r in _rows(engine)} == {0}

    recovered = RecordingPublisher()
    assert outbox.relay_once(recovered).published == 3
    assert crashing.published[0] in recovered.published  # re-published: duplicates are expected
    assert outbox.stats()["PUBLISHED"] == 3


def test_concurrent_relays_never_publish_the_same_row_twice(engine: Engine, tmp_path: Path) -> None:
    _make_runs(engine, tmp_path, 12)
    pub = RecordingPublisher(delay_s=0.05)
    barrier = threading.Barrier(3)

    def worker() -> None:
        relay = SqlOutbox(engine)
        barrier.wait()
        while relay.relay_once(pub, batch_size=3).published:
            pass

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert len(pub.published) == 12 and len(set(pub.published)) == 12
