from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine

DEFAULT_DATABASE_URL = "postgresql+psycopg://ee:ee_dev_password@127.0.0.1:5432/ee"
DEFAULT_AMQP_URL = "amqp://ee:ee_dev_password@127.0.0.1:5672//"


@dataclass(frozen=True)
class InfraSettings:
    """Infrastructure settings. The defaults match deploy/docker-compose.yml (dev only)."""

    database_url: str
    amqp_url: str
    artifact_root: Path
    learning_queue: str = "ee.learning"
    dead_letter_queue: str = "ee.learning.dead"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "InfraSettings":
        import os

        e = env if env is not None else dict(os.environ)
        return cls(
            database_url=e.get("EE_DATABASE_URL", DEFAULT_DATABASE_URL),
            amqp_url=e.get("EE_AMQP_URL", DEFAULT_AMQP_URL),
            artifact_root=Path(e.get("EE_ARTIFACT_ROOT", "runs/artifacts")),
            learning_queue=e.get("EE_LEARNING_QUEUE", "ee.learning"),
            dead_letter_queue=e.get("EE_DEAD_LETTER_QUEUE", "ee.learning.dead"),
        )


def make_engine(database_url: str) -> Engine:
    # Force UTC so timestamptz values round-trip as UTC (domain models reject non-UTC offsets).
    return create_engine(
        database_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": "-c timezone=UTC"},
    )
