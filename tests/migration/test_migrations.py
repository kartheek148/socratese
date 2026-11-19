import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from conftest import downgrade, migrate
from experience_engine.adapters.db.tables import APPEND_ONLY_TABLES, metadata

pytestmark = pytest.mark.migration

EXPECTED_TABLES = {t.name for t in metadata.sorted_tables}


def _tables(engine: sa.Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def _triggers(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(sa.text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).scalars()
        )


def test_upgrade_downgrade_upgrade_on_empty_database(scratch_database: str) -> None:
    engine = sa.create_engine(scratch_database)
    assert _tables(engine) == set()
    migrate(scratch_database)
    assert _tables(engine) == EXPECTED_TABLES
    expected_triggers = {f"{n}_append_only" for n in APPEND_ONLY_TABLES} | {
        "policy_decisions_guard"
    }
    assert expected_triggers <= _triggers(engine)

    downgrade(scratch_database)
    assert _tables(engine) == set() and _triggers(engine) == set()
    with engine.connect() as conn:  # helper functions are dropped as well
        funcs = (
            conn.execute(sa.text("SELECT proname FROM pg_proc WHERE proname LIKE 'ee\\_%'"))
            .scalars()
            .all()
        )
    assert funcs == []

    migrate(scratch_database)
    assert _tables(engine) == EXPECTED_TABLES
    engine.dispose()


def test_migration_matches_table_definitions_no_drift(scratch_database: str) -> None:
    migrate(scratch_database)
    engine = sa.create_engine(scratch_database)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    engine.dispose()
    assert diff == []


def test_schema_enforces_tenant_scoped_foreign_keys_and_checks(scratch_database: str) -> None:
    migrate(scratch_database)
    engine = sa.create_engine(scratch_database)
    insp = inspect(engine)
    for table in ("task_runs", "trace_events", "run_outcomes", "policy_decisions", "learning_jobs"):
        fks = insp.get_foreign_keys(table)
        assert any(fk["constrained_columns"][0] == "tenant_id" for fk in fks), table
    assert any(
        ix["name"] == "ix_outbox_events_pending" and ix["dialect_options"].get("postgresql_where")
        for ix in insp.get_indexes("outbox_events")
    )
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO outbox_events (event_id, tenant_id, type, aggregate_id, payload, "
                "correlation_id, status, attempts, next_attempt_at, created_at) VALUES "
                "(gen_random_uuid(), 't', 'x', 'a', '{}', 'c', 'BOGUS', 0, now(), now())"
            )
        )
    engine.dispose()


def test_populated_database_survives_noop_upgrade_and_downgrade_clears_it(
    scratch_database: str,
) -> None:
    migrate(scratch_database)
    engine = sa.create_engine(scratch_database)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tasks (tenant_id, task_id, spec, spec_hash, created_at) "
                "VALUES ('t', 'k', '{}', 'h', now())"
            )
        )
    migrate(scratch_database)  # already at head: must not touch data
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one() == 1
    downgrade(scratch_database)  # 0001 is the base schema: downgrade intentionally drops it
    assert _tables(engine) == set()
    engine.dispose()
