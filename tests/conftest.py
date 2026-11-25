"""Shared fixtures. Integration tests need real PostgreSQL / RabbitMQ and are skipped (with a
reason) when the corresponding environment variable is not set:

  EE_TEST_DATABASE_URL  admin URL, e.g. postgresql+psycopg://ee:ee_dev_password@127.0.0.1:55432/postgres
  EE_TEST_AMQP_URL      e.g. amqp://ee:ee_dev_password@127.0.0.1:56720//

Each session creates (and drops) its own database and uses uniquely named queues, so tests never
touch other databases or queues on the same servers.
"""

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from experience_engine.adapters.db.engine import InfraSettings, make_engine

ROOT = Path(__file__).resolve().parents[1]
# integration tests reuse builders from unit-test modules (basenames are unique across tests/)
sys.path.insert(0, str(Path(__file__).parent / "unit"))


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("EE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("EE_TEST_DATABASE_URL not set (PostgreSQL integration tests)")
    return url


@pytest.fixture
def scratch_database(admin_url: str) -> Iterator[str]:
    """An empty, uniquely named database (for migration tests)."""
    name = f"ee_test_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def database_url(admin_url: str) -> Iterator[str]:
    name = f"ee_test_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    migrate(url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as conn:
        # TRUNCATE is used only for test isolation; it does not fire the append-only row triggers.
        conn.execute(
            sa.text(
                "TRUNCATE tasks, task_runs, artifacts, trace_events, run_outcomes, "
                "policy_decisions, outbox_events, consumed_events, learning_jobs, "
                "dead_letters, audit_log, experience_cases, capabilities, capability_versions, "
                "capability_evidence, cost_ledger, capability_evaluations, capability_transitions, "
                "capability_pointers, rollout_observations, memories, memory_versions, "
                "memory_evaluations, memory_transitions, memory_pointers, "
                "memory_observations CASCADE"
            )
        )
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def amqp_url() -> str:
    url = os.environ.get("EE_TEST_AMQP_URL")
    if not url:
        pytest.skip("EE_TEST_AMQP_URL not set (RabbitMQ integration tests)")
    return url


@pytest.fixture
def infra(amqp_url: str, database_url: str, tmp_path: Path) -> Iterator[InfraSettings]:
    suffix = uuid.uuid4().hex[:10]
    settings = InfraSettings(
        database_url=database_url,
        amqp_url=amqp_url,
        artifact_root=tmp_path / "artifacts",
        learning_queue=f"ee_test.{suffix}.learning",
        dead_letter_queue=f"ee_test.{suffix}.dead",
    )
    yield settings
    from kombu import Connection

    with Connection(amqp_url) as conn:
        channel = conn.default_channel
        for q in (settings.learning_queue, settings.dead_letter_queue):
            channel.queue_delete(q)
        for x in (f"{settings.learning_queue}.x", f"{settings.dead_letter_queue}.x"):
            channel.exchange_delete(x)
