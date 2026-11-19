import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Engine

from experience_engine.adapters.db import tables as t
from experience_engine.adapters.db.run_lifecycle import write_audit
from experience_engine.domain.retry import RetryPolicy
from experience_engine.domain.run import OutboxEvent, OutboxStatus
from experience_engine.ports.events import EventPublisher, PublishError

log = logging.getLogger("experience_engine.outbox")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RelayResult:
    published: int = 0
    failed: int = 0
    dead_lettered: int = 0


class SqlOutbox:
    """Relays outbox rows to a broker with at-least-once delivery.

    Rows are claimed with FOR UPDATE SKIP LOCKED inside one transaction, so concurrent relays
    never publish the same row simultaneously and a crashed relay releases its rows untouched
    (they stay PENDING and are retried, which also makes this the reconciliation path for rows
    that were never published). A crash after a successful publish but before commit re-publishes
    the event: consumers must be idempotent. When the broker fails, the batch stops at the first
    failing row so a broker outage does not burn the attempt budget of the whole queue.
    """

    def __init__(
        self,
        engine: Engine,
        retry: RetryPolicy | None = None,
        now: Callable[[], datetime] = _utcnow,
        rng: Callable[[], float] | None = None,
    ):
        self._engine = engine
        self._retry = retry or RetryPolicy(max_attempts=10, base_delay_s=1.0, max_delay_s=300.0)
        self._now = now
        self._rng: Callable[[], float] = rng or random.random

    def relay_once(self, publisher: EventPublisher, batch_size: int = 50) -> RelayResult:
        published = failed = dead = 0
        now = self._now()
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(t.outbox_events)
                    .where(
                        t.outbox_events.c.status == OutboxStatus.PENDING.value,
                        t.outbox_events.c.next_attempt_at <= now,
                    )
                    .order_by(t.outbox_events.c.created_at, t.outbox_events.c.event_id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            for row in rows:
                event = OutboxEvent(
                    event_id=row["event_id"],
                    tenant_id=row["tenant_id"],
                    type=row["type"],
                    aggregate_id=row["aggregate_id"],
                    payload=row["payload"],
                    correlation_id=row["correlation_id"],
                    created_at=row["created_at"],
                    attempts=row["attempts"],
                )
                try:
                    publisher.publish(event)
                except PublishError as exc:
                    attempts = row["attempts"] + 1
                    error = str(exc)[:500]
                    log.warning(
                        "outbox publish failed",
                        extra={"correlation_id": event.correlation_id, "attempts": attempts},
                    )
                    values: dict[str, object] = {"attempts": attempts, "last_error": error}
                    if self._retry.exhausted(attempts):
                        values["status"] = OutboxStatus.DEAD_LETTERED.value
                        dead += 1
                        write_audit(
                            conn,
                            tenant_id=event.tenant_id,
                            action="outbox.dead_lettered",
                            entity_type="outbox_event",
                            entity_id=event.event_id,
                            correlation_id=event.correlation_id,
                            ts=now,
                            detail={"attempts": attempts, "error": error},
                        )
                    else:
                        delay = self._retry.delay_s(attempts, self._rng)
                        values["next_attempt_at"] = now + timedelta(seconds=delay)
                    conn.execute(
                        t.outbox_events.update()
                        .where(t.outbox_events.c.event_id == event.event_id)
                        .values(**values)
                    )
                    failed += 1
                    break
                conn.execute(
                    t.outbox_events.update()
                    .where(t.outbox_events.c.event_id == event.event_id)
                    .values(
                        status=OutboxStatus.PUBLISHED.value,
                        published_at=now,
                        attempts=row["attempts"] + 1,
                        last_error=None,
                    )
                )
                published += 1
        return RelayResult(published=published, failed=failed, dead_lettered=dead)

    def stats(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(t.outbox_events.c.status, sa.func.count()).group_by(
                    t.outbox_events.c.status
                )
            ).all()
        counts = {s.value: 0 for s in OutboxStatus}
        counts.update({status: n for status, n in rows})
        return counts

    def requeue_dead_letter(self, tenant_id: str, event_id: str) -> bool:
        """Operator action: make a dead-lettered row publishable again (audited)."""
        now = self._now()
        with self._engine.begin() as conn:
            res = conn.execute(
                t.outbox_events.update()
                .where(
                    t.outbox_events.c.event_id == event_id,
                    t.outbox_events.c.tenant_id == tenant_id,
                    t.outbox_events.c.status == OutboxStatus.DEAD_LETTERED.value,
                )
                .values(status=OutboxStatus.PENDING.value, attempts=0, next_attempt_at=now)
                .returning(t.outbox_events.c.correlation_id)
            )
            row = res.first()
            if row is None:
                return False
            write_audit(
                conn,
                tenant_id=tenant_id,
                action="outbox.requeued",
                entity_type="outbox_event",
                entity_id=event_id,
                correlation_id=row[0],
                ts=now,
                actor="operator",
            )
            return True
