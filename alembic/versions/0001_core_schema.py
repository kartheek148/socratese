"""core schema

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_artifacts_sha256_hex")),
        sa.PrimaryKeyConstraint("tenant_id", "sha256", name=op.f("pk_artifacts")),
    )
    op.create_table(
        "audit_log",
        sa.Column("audit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("audit_id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_log_tenant_id_entity_type_entity_id",
        "audit_log",
        ["tenant_id", "entity_type", "entity_id"],
        unique=False,
    )
    op.create_table(
        "consumed_events",
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("consumer", "event_id", name=op.f("pk_consumed_events")),
    )
    op.create_table(
        "dead_letters",
        sa.Column("dead_letter_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(raw) <= 4096", name=op.f("ck_dead_letters_raw_bounded")),
        sa.PrimaryKeyConstraint("dead_letter_id", name=op.f("pk_dead_letters")),
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','PUBLISHED','DEAD_LETTERED')",
            name=op.f("ck_outbox_events_status"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_outbox_events_tenant_id_aggregate_id",
        "outbox_events",
        ["tenant_id", "aggregate_id"],
        unique=False,
    )
    op.create_table(
        "tasks",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "task_id", name=op.f("pk_tasks")),
    )
    op.create_table(
        "task_runs",
        sa.Column("run_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("config_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("runtime_version", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=True),
        sa.Column("model_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING','COMPLETED')", name=op.f("ck_task_runs_status")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name=op.f("fk_task_runs_tenant_id_task_id"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_task_runs")),
        sa.UniqueConstraint("tenant_id", "run_id", name=op.f("uq_task_runs_tenant_id_run_id")),
    )
    op.create_index(
        "ix_task_runs_tenant_id_started_at", "task_runs", ["tenant_id", "started_at"], unique=False
    )
    op.create_table(
        "learning_jobs",
        sa.Column("job_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("outcome_status", sa.Text(), nullable=False),
        sa.Column("eligible_for_success_mining", sa.Boolean(), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "NOT eligible_for_success_mining OR outcome_status = 'VERIFIED_SUCCESS'",
            name=op.f("ck_learning_jobs_mining_requires_verified"),
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name=op.f("ck_learning_jobs_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["task_runs.tenant_id", "task_runs.run_id"],
            name=op.f("fk_learning_jobs_tenant_id_run_id"),
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_learning_jobs")),
        sa.UniqueConstraint(
            "tenant_id",
            "job_type",
            "dedupe_key",
            name=op.f("uq_learning_jobs_tenant_id_job_type_dedupe_key"),
        ),
    )
    op.create_table(
        "policy_decisions",
        sa.Column("decision_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("feature_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("eligible_actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("estimates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("selected_action", sa.Text(), nullable=False),
        sa.Column("selection_probability", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("outcome_status", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome_status IS NULL OR outcome_status IN ('VERIFIED_SUCCESS','FAILURE','PARTIAL','UNKNOWN')",
            name=op.f("ck_policy_decisions_outcome_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["task_runs.tenant_id", "task_runs.run_id"],
            name=op.f("fk_policy_decisions_tenant_id_run_id"),
        ),
        sa.PrimaryKeyConstraint("decision_id", name=op.f("pk_policy_decisions")),
    )
    op.create_index(
        "ix_policy_decisions_tenant_id_run_id",
        "policy_decisions",
        ["tenant_id", "run_id"],
        unique=False,
    )
    op.create_table(
        "run_outcomes",
        sa.Column("run_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("config_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("capability_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("verification_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("execution_time_ms", sa.Integer(), nullable=False),
        sa.Column("human_interventions", sa.Integer(), nullable=False),
        sa.Column("failure_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verifier_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('VERIFIED_SUCCESS','FAILURE','PARTIAL','UNKNOWN')",
            name=op.f("ck_run_outcomes_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["task_runs.tenant_id", "task_runs.run_id"],
            name=op.f("fk_run_outcomes_tenant_id_run_id"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_outcomes")),
    )
    op.create_table(
        "trace_events",
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("parent_event_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("component_version", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(artifact_refs::text) <= 16384", name=op.f("ck_trace_events_refs_bounded")
        ),
        sa.CheckConstraint(
            "octet_length(metrics::text) <= 8192", name=op.f("ck_trace_events_metrics_bounded")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "parent_event_id"],
            ["trace_events.tenant_id", "trace_events.run_id", "trace_events.event_id"],
            name=op.f("fk_trace_events_tenant_id_run_id_parent_event_id"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["task_runs.tenant_id", "task_runs.run_id"],
            name=op.f("fk_trace_events_tenant_id_run_id"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_trace_events")),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "event_id",
            name=op.f("uq_trace_events_tenant_id_run_id_event_id"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "sequence",
            name=op.f("uq_trace_events_tenant_id_run_id_sequence"),
        ),
    )

    # Append-only enforcement. Privileged roles can still TRUNCATE/DROP: production application
    # roles must not be granted those privileges.
    op.execute(
        """
        CREATE FUNCTION ee_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
        END $$
        """
    )
    for table in ("trace_events", "run_outcomes", "audit_log", "artifacts"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ee_reject_mutation()"
        )

    # A decision row is immutable except for the one-time back-fill of its verified outcome.
    op.execute(
        """
        CREATE FUNCTION ee_policy_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'policy_decisions rows cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.outcome_status IS NOT NULL
               OR (to_jsonb(NEW) - 'outcome_status') IS DISTINCT FROM (to_jsonb(OLD) - 'outcome_status') THEN
                RAISE EXCEPTION 'policy_decisions allow only a one-time outcome_status back-fill' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER policy_decisions_guard BEFORE UPDATE OR DELETE ON policy_decisions "
        "FOR EACH ROW EXECUTE FUNCTION ee_policy_decision_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER policy_decisions_guard ON policy_decisions")
    op.execute("DROP FUNCTION ee_policy_decision_guard()")
    for table in ("trace_events", "run_outcomes", "audit_log", "artifacts"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION ee_reject_mutation()")

    op.drop_table("trace_events")
    op.drop_table("run_outcomes")
    op.drop_index("ix_policy_decisions_tenant_id_run_id", table_name="policy_decisions")
    op.drop_table("policy_decisions")
    op.drop_table("learning_jobs")
    op.drop_index("ix_task_runs_tenant_id_started_at", table_name="task_runs")
    op.drop_table("task_runs")
    op.drop_table("tasks")
    op.drop_index("ix_outbox_events_tenant_id_aggregate_id", table_name="outbox_events")
    op.drop_index(
        "ix_outbox_events_pending",
        table_name="outbox_events",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_table("outbox_events")
    op.drop_table("dead_letters")
    op.drop_table("consumed_events")
    op.drop_index("ix_audit_log_tenant_id_entity_type_entity_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("artifacts")
