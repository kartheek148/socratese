from typing import Protocol

from experience_engine.domain.run import OutboxEvent


class PublishError(Exception):
    """Broker unavailable or rejected the message; the outbox row stays pending and is retried."""


class EventPublisher(Protocol):
    def publish(self, event: OutboxEvent) -> None:
        """At-least-once: the same event may be published more than once."""
        ...
