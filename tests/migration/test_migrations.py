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


def test_0005_backfills_acted_true_and_downgrades_cleanly(scratch_database: str) -> None:
    migrate(scratch_database, "0004")
    engine = sa.create_engine(scratch_database)
    run_id = "018f0000-0000-7000-8000-000000000001"
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tasks (tenant_id, task_id, spec, spec_hash, created_at) "
                "VALUES ('t', 'k', '{}', 'h', now())"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO task_runs (run_id, tenant_id, task_id, correlation_id, config_id, "
                "policy_version, runtime_version, model_settings, tags, status, started_at) "
                "VALUES (:r, 't', 'k', 'c', 'cfg', 'p', 'v', '{}', '{}', 'RUNNING', now())"
            ),
            {"r": run_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO policy_decisions (decision_id, tenant_id, run_id, policy_version, "
                "feature_snapshot, eligible_actions, estimates, selected_action, reason, "
                "created_at) "
                "VALUES (gen_random_uuid(), 't', :r, 'rule-capability/1', '{}', '[]', '{}', "
                "'GENERAL_PLANNING', 'legacy', now())"
            ),
            {"r": run_id},
        )
    migrate(scratch_database)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT acted FROM policy_decisions")).scalar_one() is True
        assert "controller_policies" in set(inspect(engine).get_table_names())
        assert "controller_policies_guard" in _triggers(engine)
    downgrade(scratch_database, "0004")
    with engine.connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("policy_decisions")}
        assert "acted" not in cols
        assert conn.execute(sa.text("SELECT count(*) FROM policy_decisions")).scalar_one() == 1
    assert "controller_policies" not in _tables(engine)
    engine.dispose()


def _insert_policy(conn: sa.Connection, kind: str, version: int) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO controller_policies (version_id, tenant_id, policy_id, version, kind, "
            "state, definition, definition_hash, created_at) VALUES (gen_random_uuid(), 't', 'p', "
            ":v, :k, 'CANDIDATE', '{}', 'h', now())"
        ),
        {"v": version, "k": kind},
    )


def test_0006_allows_the_evidence_driven_kind_and_never_rewrites_history(
    scratch_database: str,
) -> None:
    migrate(scratch_database, "0005")
    engine = sa.create_engine(scratch_database)
    with engine.begin() as conn:
        _insert_policy(conn, "BANDIT", 1)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _insert_policy(conn, "EVIDENCE_DRIVEN", 2)  # not allowed before 0006
    migrate(scratch_database)
    with engine.begin() as conn:
        _insert_policy(conn, "EVIDENCE_DRIVEN", 2)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _insert_policy(conn, "UNKNOWN_KIND", 3)
    with pytest.raises(sa.exc.DBAPIError, match="not rewriting history"):
        downgrade(scratch_database, "0005")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM controller_policies")).scalar_one() == 2
    with engine.begin() as conn:  # only with the evidence-driven rows gone can it step back
        conn.execute(sa.text("ALTER TABLE controller_policies DISABLE TRIGGER ALL"))
        conn.execute(sa.text("DELETE FROM controller_policies WHERE kind = 'EVIDENCE_DRIVEN'"))
        conn.execute(sa.text("ALTER TABLE controller_policies ENABLE TRIGGER ALL"))
    downgrade(scratch_database, "0005")
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        _insert_policy(conn, "EVIDENCE_DRIVEN", 4)
    engine.dispose()
