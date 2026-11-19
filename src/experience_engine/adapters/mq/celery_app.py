"""Celery/RabbitMQ adapter: publisher, consumer task, queue topology.

Reliability model: at-least-once. The task is acks_late, retries transient failures with bounded
exponential backoff + jitter, and on exhaustion or a poison message records a `dead_letters` row
and rejects the message without requeue so RabbitMQ routes it to the dead-letter queue.
"""

import json
import logging
from typing import Any

from celery import Celery
from celery.exceptions import Reject
from kombu.entity import Exchange, Queue
from sqlalchemy.exc import IntegrityError

from experience_engine.adapters.db.engine import InfraSettings
from experience_engine.domain.retry import RetryPolicy
from experience_engine.domain.run import EXECUTION_COMPLETED, OutboxEvent
from experience_engine.learning.collector import ExecutionCompletedHandler, NonRetryableEventError
from experience_engine.ports.events import PublishError

log = logging.getLogger("experience_engine.mq")

TASK_NAME = "ee.learning.process_execution_completed"


def topology(settings: InfraSettings) -> tuple[Queue, Queue]:
    dead_exchange = Exchange(f"{settings.dead_letter_queue}.x", "direct", durable=True)
    dead = Queue(
        settings.dead_letter_queue,
        exchange=dead_exchange,
        routing_key=settings.dead_letter_queue,
        durable=True,
    )
    main = Queue(
        settings.learning_queue,
        exchange=Exchange(f"{settings.learning_queue}.x", "direct", durable=True),
        routing_key=settings.learning_queue,
        durable=True,
        queue_arguments={
            "x-dead-letter-exchange": dead_exchange.name,
            "x-dead-letter-routing-key": settings.dead_letter_queue,
        },
    )
    return main, dead


def ensure_topology(app: Celery, settings: InfraSettings) -> None:
    """Declare the dead-letter queue BEFORE the main queue so rejected messages are never lost."""
    main, dead = topology(settings)
    with app.connection_for_write() as conn:
        channel = conn.default_channel
        dead(channel).declare()
        main(channel).declare()


def create_celery_app(
    settings: InfraSettings,
    handler: ExecutionCompletedHandler,
    retry: RetryPolicy | None = None,
) -> Celery:
    policy = retry or RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=60.0)
    app = Celery("experience_engine", broker=settings.amqp_url)
    main, _dead = topology(settings)
    app.conf.update(
        task_queues=[main],
        task_default_queue=settings.learning_queue,
        task_default_exchange=main.exchange.name,
        task_default_routing_key=settings.learning_queue,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        broker_transport_options={"confirm_publish": True},
        task_track_started=False,
    )

    @app.task(bind=True, name=TASK_NAME, shared=False)  # type: ignore[untyped-decorator]
    def process_execution_completed(self: Any, payload: dict[str, Any]) -> str:
        correlation_id = str(payload.get("correlation_id", "unknown"))
        raw_text = json.dumps(payload, sort_keys=True, default=str)
        attempts = int(self.request.retries) + 1
        try:
            result = handler.handle(payload)
        except Exception as exc:
            poison = isinstance(exc, NonRetryableEventError | IntegrityError)
            if poison or policy.exhausted(attempts):
                log.error(
                    "dead-lettering message",
                    extra={"correlation_id": correlation_id, "attempts": attempts},
                )
                try:
                    handler.dead_letter(payload, f"{type(exc).__name__}: {exc}", attempts, raw_text)
                except Exception:  # the broker DLQ copy still preserves the message
                    log.exception("could not record dead letter in the database")
                raise Reject(str(exc)[:200], requeue=False) from exc
            log.warning(
                "retrying message",
                extra={"correlation_id": correlation_id, "attempts": attempts},
            )
            raise self.retry(
                exc=exc, countdown=policy.delay_s(attempts), max_retries=policy.max_attempts
            ) from exc
        log.info(
            "processed execution.completed",
            extra={"correlation_id": correlation_id, "result": result.value},
        )
        return result.value

    return app


class CeleryEventPublisher:
    def __init__(self, app: Celery, settings: InfraSettings):
        self._app = app
        self._settings = settings
        self._topology_ready = False

    def publish(self, event: OutboxEvent) -> None:
        if event.type != EXECUTION_COMPLETED:
            raise PublishError(f"no route for event type {event.type!r}")
        try:
            if not self._topology_ready:
                ensure_topology(self._app, self._settings)
                self._topology_ready = True
            self._app.send_task(
                TASK_NAME,
                args=[event.payload],
                queue=self._settings.learning_queue,
                task_id=event.event_id,
                headers={"correlation_id": event.correlation_id},
                retry=True,
                retry_policy={"max_retries": 2, "interval_start": 0.1, "interval_step": 0.2},
            )
        except Exception as exc:
            self._topology_ready = False
            raise PublishError(f"{type(exc).__name__}: {exc}"[:500]) from exc
