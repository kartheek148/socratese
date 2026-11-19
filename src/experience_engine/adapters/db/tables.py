"""SQLAlchemy Core table definitions (source of truth for tests; the Alembic migration is written
explicitly and a test asserts there is no drift between the two).

Conventions: tenant_id on every tenant-owned table; child tables reference parents through
composite (tenant_id, id) foreign keys so cross-tenant references are impossible; all timestamps
are timestamptz (UTC); ids are UUIDs (application-generated UUIDv7).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = sa.MetaData(naming_convention=NAMING)


def _uuid() -> sa.types.TypeEngine[str]:
    return sa.Uuid(as_uuid=False)


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True)


tasks = sa.Table(
    "tasks",
    metadata,
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("spec", JSONB, nullable=False),
    sa.Column("spec_hash", sa.Text, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "task_id"),
)

task_runs = sa.Table(
    "task_runs",
    metadata,
    sa.Column("run_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.Column("config_id", sa.Text, nullable=False),
    sa.Column("policy_version", sa.Text, nullable=False),
    sa.Column("runtime_version", sa.Text, nullable=False),
    sa.Column("analyzer_version", sa.Text),
    sa.Column("model_settings", JSONB, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("started_at", _ts(), nullable=False),
    sa.Column("ended_at", _ts()),
    sa.UniqueConstraint("tenant_id", "run_id"),
    sa.ForeignKeyConstraint(["tenant_id", "task_id"], ["tasks.tenant_id", "tasks.task_id"]),
    sa.CheckConstraint("status IN ('RUNNING','COMPLETED')", name="status"),
    sa.Index("ix_task_runs_tenant_id_started_at", "tenant_id", "started_at"),
)

artifacts = sa.Table(
    "artifacts",
    metadata,
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("uri", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("content_type", sa.Text, nullable=False),
    sa.Column("redacted", sa.Boolean, nullable=False),
    sa.Column("truncated", sa.Boolean, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "sha256"),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
)

trace_events = sa.Table(
    "trace_events",
    metadata,
    sa.Column("event_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("run_id", _uuid(), nullable=False),
    sa.Column("trace_id", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("parent_event_id", _uuid()),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("component", sa.Text, nullable=False),
    sa.Column("component_version", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("metrics", JSONB, nullable=False),
    sa.Column("artifact_refs", JSONB, nullable=False),
    sa.Column("ts", _ts(), nullable=False),
    sa.UniqueConstraint("tenant_id", "run_id", "sequence"),
    sa.UniqueConstraint("tenant_id", "run_id", "event_id"),
    sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["task_runs.tenant_id", "task_runs.run_id"]),
    sa.ForeignKeyConstraint(
        ["tenant_id", "run_id", "parent_event_id"],
        ["trace_events.tenant_id", "trace_events.run_id", "trace_events.event_id"],
    ),
    sa.CheckConstraint("octet_length(metrics::text) <= 8192", name="metrics_bounded"),
    sa.CheckConstraint("octet_length(artifact_refs::text) <= 16384", name="refs_bounded"),
)

run_outcomes = sa.Table(
    "run_outcomes",
    metadata,
    sa.Column("run_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("config_id", sa.Text, nullable=False),
    sa.Column("policy_version", sa.Text, nullable=False),
    sa.Column("capability_version", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("quality_score", sa.Float),
    sa.Column("verification_results", JSONB, nullable=False),
    sa.Column("tokens_used", sa.Integer, nullable=False),
    sa.Column("tool_calls", sa.Integer, nullable=False),
    sa.Column("execution_time_ms", sa.Integer, nullable=False),
    sa.Column("human_interventions", sa.Integer, nullable=False),
    sa.Column("failure_reasons", JSONB, nullable=False),
    sa.Column("evidence_refs", JSONB, nullable=False),
    sa.Column("verifier_version", sa.Text, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["task_runs.tenant_id", "task_runs.run_id"]),
    sa.CheckConstraint(
        "status IN ('VERIFIED_SUCCESS','FAILURE','PARTIAL','UNKNOWN')", name="status"
    ),
)

policy_decisions = sa.Table(
    "policy_decisions",
    metadata,
    sa.Column("decision_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("run_id", _uuid(), nullable=False),
    sa.Column("policy_version", sa.Text, nullable=False),
    sa.Column("feature_snapshot", JSONB, nullable=False),
    sa.Column("eligible_actions", JSONB, nullable=False),
    sa.Column("estimates", JSONB, nullable=False),
    sa.Column("selected_action", sa.Text, nullable=False),
    sa.Column("selection_probability", sa.Float),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("outcome_status", sa.Text),
    sa.Column("created_at", _ts(), nullable=False),
    sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["task_runs.tenant_id", "task_runs.run_id"]),
    sa.CheckConstraint(
        "outcome_status IS NULL OR outcome_status IN "
        "('VERIFIED_SUCCESS','FAILURE','PARTIAL','UNKNOWN')",
        name="outcome_status",
    ),
    sa.Index("ix_policy_decisions_tenant_id_run_id", "tenant_id", "run_id"),
)

outbox_events = sa.Table(
    "outbox_events",
    metadata,
    sa.Column("event_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("aggregate_id", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("next_attempt_at", _ts(), nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.Column("published_at", _ts()),
    sa.Column("last_error", sa.Text),
    sa.CheckConstraint("status IN ('PENDING','PUBLISHED','DEAD_LETTERED')", name="status"),
    sa.Index(
        "ix_outbox_events_pending",
        "next_attempt_at",
        postgresql_where=sa.text("status = 'PENDING'"),
    ),
    sa.Index("ix_outbox_events_tenant_id_aggregate_id", "tenant_id", "aggregate_id"),
)

consumed_events = sa.Table(
    "consumed_events",
    metadata,
    sa.Column("consumer", sa.Text, nullable=False),
    sa.Column("event_id", _uuid(), nullable=False),
    sa.Column("consumed_at", _ts(), nullable=False),
    sa.PrimaryKeyConstraint("consumer", "event_id"),
)

learning_jobs = sa.Table(
    "learning_jobs",
    metadata,
    sa.Column("job_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("job_type", sa.Text, nullable=False),
    sa.Column("dedupe_key", sa.Text, nullable=False),
    sa.Column("run_id", _uuid(), nullable=False),
    sa.Column("outcome_status", sa.Text, nullable=False),
    sa.Column("eligible_for_success_mining", sa.Boolean, nullable=False),
    sa.Column("pipeline_version", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.Column("updated_at", _ts(), nullable=False),
    sa.Column("last_error", sa.Text),
    sa.UniqueConstraint("tenant_id", "job_type", "dedupe_key"),
    sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["task_runs.tenant_id", "task_runs.run_id"]),
    sa.CheckConstraint("state IN ('PENDING','RUNNING','SUCCEEDED','FAILED')", name="state"),
    sa.CheckConstraint(
        "NOT eligible_for_success_mining OR outcome_status = 'VERIFIED_SUCCESS'",
        name="mining_requires_verified",
    ),
)

dead_letters = sa.Table(
    "dead_letters",
    metadata,
    sa.Column("dead_letter_id", _uuid(), primary_key=True),
    sa.Column("consumer", sa.Text, nullable=False),
    sa.Column("event_id", _uuid()),
    sa.Column("tenant_id", sa.Text),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("raw", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.CheckConstraint("char_length(raw) <= 4096", name="raw_bounded"),
)

audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("audit_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("entity_type", sa.Text, nullable=False),
    sa.Column("entity_id", sa.Text, nullable=False),
    sa.Column("detail", JSONB, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.Column("ts", _ts(), nullable=False),
    sa.Index(
        "ix_audit_log_tenant_id_entity_type_entity_id", "tenant_id", "entity_type", "entity_id"
    ),
)

APPEND_ONLY_TABLES = ("trace_events", "run_outcomes", "audit_log", "artifacts")
