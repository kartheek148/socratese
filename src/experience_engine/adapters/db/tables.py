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
    sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
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

experience_cases = sa.Table(
    "experience_cases",
    metadata,
    sa.Column("case_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("run_id", _uuid(), nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("pipeline_version", sa.Text, nullable=False),
    sa.Column("task_family", sa.Text),
    sa.Column("outcome_status", sa.Text, nullable=False),
    sa.Column("dataset_split", sa.Text),
    sa.Column("eligible", sa.Boolean, nullable=False),
    sa.Column("excluded_reason", sa.Text),
    sa.Column("features", JSONB, nullable=False),
    sa.Column("trajectory_sha256", sa.Text),
    sa.Column("n_steps", sa.Integer, nullable=False),
    sa.Column("tokens_used", sa.Integer, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.UniqueConstraint("tenant_id", "run_id", "pipeline_version"),
    sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["task_runs.tenant_id", "task_runs.run_id"]),
    sa.CheckConstraint(
        "outcome_status IN ('VERIFIED_SUCCESS','FAILURE','PARTIAL','UNKNOWN')",
        name="outcome_status",
    ),
    sa.CheckConstraint("eligible OR excluded_reason IS NOT NULL", name="exclusion_has_reason"),
    # Database-level guard for the learning boundary: only discovery runs with a verified
    # success/failure outcome can ever be marked eligible. `IS TRUE` matters: a NULL split
    # would otherwise evaluate to NULL, which a CHECK constraint treats as passing.
    sa.CheckConstraint(
        "NOT eligible OR ((dataset_split = 'DISCOVERY') IS TRUE "
        "AND outcome_status IN ('VERIFIED_SUCCESS','FAILURE'))",
        name="eligible_requires_discovery",
    ),
    sa.Index("ix_experience_cases_tenant_id_task_family", "tenant_id", "task_family"),
)

capabilities = sa.Table(
    "capabilities",
    metadata,
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("task_family", sa.Text, nullable=False),
    sa.Column("skeleton_signature", sa.Text, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "capability_id"),
    sa.UniqueConstraint("tenant_id", "skeleton_signature"),
)

CAPABILITY_STATES = (
    "DISCOVERED",
    "CANDIDATE",
    "VALIDATING",
    "APPROVED",
    "SHADOW",
    "CANARY",
    "ACTIVE",
    "REJECTED",
    "DEPRECATED",
    "ROLLED_BACK",
)

capability_versions = sa.Table(
    "capability_versions",
    metadata,
    sa.Column("version_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("definition", JSONB, nullable=False),
    sa.Column("definition_hash", sa.Text, nullable=False),
    sa.Column("procedure_hash", sa.Text, nullable=False),
    sa.Column("tests", JSONB, nullable=False),
    sa.Column("compiler_version", sa.Text, nullable=False),
    sa.Column("prompt_version", sa.Text),
    sa.Column("model_version", sa.Text),
    sa.Column("created_at", _ts(), nullable=False),
    sa.UniqueConstraint("tenant_id", "capability_id", "version"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "capability_id"], ["capabilities.tenant_id", "capabilities.capability_id"]
    ),
    sa.CheckConstraint(
        "state IN (" + ",".join(f"'{s}'" for s in CAPABILITY_STATES) + ")", name="state"
    ),
    sa.CheckConstraint("version >= 1", name="version_positive"),
    sa.Index("ix_capability_versions_tenant_id_state", "tenant_id", "state"),
    # at most one ACTIVE version per capability, enforced by the database
    sa.Index(
        "uq_capability_versions_one_active",
        "tenant_id",
        "capability_id",
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    ),
)

capability_evidence = sa.Table(
    "capability_evidence",
    metadata,
    sa.Column("evidence_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("run_id", _uuid(), nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.UniqueConstraint(
        "tenant_id", "capability_id", "version", "run_id", "role", name="uq_capability_evidence_key"
    ),
    sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["task_runs.tenant_id", "task_runs.run_id"]),
    sa.ForeignKeyConstraint(
        ["tenant_id", "capability_id", "version"],
        [
            "capability_versions.tenant_id",
            "capability_versions.capability_id",
            "capability_versions.version",
        ],
    ),
    sa.CheckConstraint("role IN ('SOURCE_SUCCESS','COUNTEREXAMPLE')", name="role"),
)

cost_ledger = sa.Table(
    "cost_ledger",
    metadata,
    sa.Column("entry_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("pipeline", sa.Text, nullable=False),
    sa.Column("subject_type", sa.Text, nullable=False),
    sa.Column("subject_id", sa.Text, nullable=False),
    sa.Column("tokens", sa.Integer, nullable=False),
    sa.Column("tool_calls", sa.Integer, nullable=False),
    sa.Column("wall_ms", sa.Integer, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.Column("created_at", _ts(), nullable=False),
    sa.Index("ix_cost_ledger_tenant_id_pipeline", "tenant_id", "pipeline"),
)

_VERSION_FK = (
    ["tenant_id", "capability_id", "version"],
    [
        "capability_versions.tenant_id",
        "capability_versions.capability_id",
        "capability_versions.version",
    ],
)

capability_evaluations = sa.Table(
    "capability_evaluations",
    metadata,
    sa.Column("evaluation_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("verdict", sa.Text, nullable=False),
    sa.Column("risk_class", sa.Text, nullable=False),
    sa.Column("policy_name", sa.Text, nullable=False),
    sa.Column("policy_hash", sa.Text, nullable=False),
    sa.Column("policy", JSONB, nullable=False),
    sa.Column("source_replay_n", sa.Integer, nullable=False),
    sa.Column("heldout_n", sa.Integer, nullable=False),
    sa.Column("transfer_n", sa.Integer, nullable=False),
    sa.Column("negative_n", sa.Integer, nullable=False),
    sa.Column("regression_n", sa.Integer, nullable=False),
    sa.Column("metrics", JSONB, nullable=False),
    sa.Column("reasons", JSONB, nullable=False),
    sa.Column("dataset_versions", JSONB, nullable=False),
    sa.Column("model_settings", JSONB, nullable=False),
    sa.Column("seeds", JSONB, nullable=False),
    sa.Column("runtime_version", sa.Text, nullable=False),
    sa.Column("verifier_version", sa.Text, nullable=False),
    sa.Column("environment_version", sa.Text, nullable=False),
    sa.Column("evaluator_version", sa.Text, nullable=False),
    sa.Column("report_sha256", sa.Text, nullable=False),
    sa.Column("started_at", _ts(), nullable=False),
    sa.Column("completed_at", _ts(), nullable=False),
    sa.ForeignKeyConstraint(*_VERSION_FK),
    sa.CheckConstraint("verdict IN ('PASS','FAIL','INCONCLUSIVE')", name="verdict"),
    # Source-task replay alone can never produce a PASS: held-out, transfer and negative
    # applicability evidence must exist.
    sa.CheckConstraint(
        "verdict <> 'PASS' OR (heldout_n > 0 AND transfer_n > 0 AND negative_n > 0)",
        name="pass_requires_heldout_transfer_negative",
    ),
    sa.Index("ix_capability_evaluations_version", "tenant_id", "capability_id", "version"),
)

capability_transitions = sa.Table(
    "capability_transitions",
    metadata,
    sa.Column("transition_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("from_state", sa.Text, nullable=False),
    sa.Column("to_state", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("evaluation_id", _uuid()),
    sa.Column("policy_hash", sa.Text),
    sa.Column("created_at", _ts(), nullable=False),
    sa.ForeignKeyConstraint(*_VERSION_FK),
    sa.ForeignKeyConstraint(["evaluation_id"], ["capability_evaluations.evaluation_id"]),
    sa.CheckConstraint("char_length(reason) <= 500", name="reason_bounded"),
    sa.Index("ix_capability_transitions_version", "tenant_id", "capability_id", "version"),
)

capability_pointers = sa.Table(
    "capability_pointers",
    metadata,
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("active_version", sa.Integer),
    sa.Column("previous_version", sa.Integer),
    sa.Column("updated_at", _ts(), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "capability_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "capability_id"], ["capabilities.tenant_id", "capabilities.capability_id"]
    ),
)

rollout_observations = sa.Table(
    "rollout_observations",
    metadata,
    sa.Column("observation_id", _uuid(), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("mode", sa.Text, nullable=False),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("capability_verified", sa.Boolean, nullable=False),
    sa.Column("baseline_verified", sa.Boolean),
    sa.Column("capability_tokens", sa.Integer, nullable=False),
    sa.Column("baseline_tokens", sa.Integer),
    sa.Column("created_at", _ts(), nullable=False),
    sa.ForeignKeyConstraint(*_VERSION_FK),
    sa.CheckConstraint("mode IN ('SHADOW','CANARY')", name="mode"),
    sa.Index(
        "ix_rollout_observations_version_mode", "tenant_id", "capability_id", "version", "mode"
    ),
)

APPEND_ONLY_TABLES = (
    "trace_events",
    "run_outcomes",
    "audit_log",
    "artifacts",
    "experience_cases",
    "capability_evidence",
    "cost_ledger",
    "capability_evaluations",
    "capability_transitions",
    "rollout_observations",
)
