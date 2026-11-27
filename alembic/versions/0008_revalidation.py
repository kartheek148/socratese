"""controlled revalidation: append-only case transitions and trial evidence (M6.3)

Revision ID: 0008
Revises: 0007

Two new tables; nothing existing changes. Rows are audit evidence: database triggers reject UPDATE
and DELETE, and the downgrade refuses to drop a table that holds rows (history is not rewritten).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRANSITIONS = "controller_revalidation_transitions"
TRIALS = "controller_revalidation_trials"
TABLES = (TRANSITIONS, TRIALS)


def upgrade() -> None:
    op.create_table(
        "controller_revalidation_transitions",
        sa.Column("transition_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("cell_key", sa.Text(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("measurements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rebase", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("n_trials", sa.Integer(), nullable=False),
        sa.Column("n_unverified", sa.Integer(), nullable=False),
        sa.Column("tokens_cycle", sa.Integer(), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("params_hash", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("log_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_state IS NULL OR from_state IN ('QUARANTINED','REVALIDATION_PENDING','REVALIDATING','RECOVERY_CANDIDATE','RECOVERED','EXPIRED','BUDGET_EXHAUSTED')",
            name=op.f("ck_controller_revalidation_transitions_from_state"),
        ),
        sa.CheckConstraint(
            "reason IN ('STATISTICAL_DRIFT','PERFORMANCE_DEGRADATION','COST_DEGRADATION','VERIFICATION_FAILURE','STRUCTURAL_INVALIDATION','EVIDENCE_STALE','UNKNOWN')",
            name=op.f("ck_controller_revalidation_transitions_reason"),
        ),
        sa.CheckConstraint(
            "strategy IN ('FIXED','EVIDENCE_THRESHOLD','SEQUENTIAL')",
            name=op.f("ck_controller_revalidation_transitions_strategy"),
        ),
        sa.CheckConstraint(
            "to_state IN ('QUARANTINED','REVALIDATION_PENDING','REVALIDATING','RECOVERY_CANDIDATE','RECOVERED','EXPIRED','BUDGET_EXHAUSTED')",
            name=op.f("ck_controller_revalidation_transitions_to_state"),
        ),
        sa.CheckConstraint(
            "trigger IN ('CASE_OPENED','INELIGIBLE','READY','TRIALS_STARTED','CRITERIA_MET','CRITERIA_NOT_MET','CONFIRMED','CONFIRMATION_FAILED','VERIFICATION_UNAVAILABLE','LINEAGE_VIOLATION','CYCLES_EXHAUSTED','TRIAL_CAP','TOKEN_CAP','LIFETIME_BUDGET','TTL_EXPIRED','CYCLE_EXPIRED')",
            name=op.f("ck_controller_revalidation_transitions_trigger"),
        ),
        sa.CheckConstraint(
            "seq >= 1", name=op.f("ck_controller_revalidation_transitions_seq_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id", "version"],
            [
                "controller_policies.tenant_id",
                "controller_policies.policy_id",
                "controller_policies.version",
            ],
            name=op.f("fk_controller_revalidation_transitions_tenant_id_policy_id_version"),
        ),
        sa.PrimaryKeyConstraint(
            "transition_id", name=op.f("pk_controller_revalidation_transitions")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version",
            "cell_key",
            "seq",
            name=op.f(
                "uq_controller_revalidation_transitions_tenant_id_policy_id_version_cell_key_seq"
            ),
        ),
    )
    op.create_index(
        "ix_controller_revalidation_transitions_version",
        "controller_revalidation_transitions",
        ["tenant_id", "policy_id", "version"],
        unique=False,
    )
    op.create_table(
        "controller_revalidation_trials",
        sa.Column("trial_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("cell_key", sa.Text(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=False),
        sa.Column("round_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_ref", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("runtime_version", sa.Text(), nullable=True),
        sa.Column("verifier_version", sa.Text(), nullable=True),
        sa.Column("capability_ref", sa.Text(), nullable=True),
        sa.Column("result_tenant_id", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reject_reason IS NULL OR reject_reason IN ('NOT_NEWER','TENANT_MISMATCH','DUPLICATE','VERSION_MISMATCH')",
            name=op.f("ck_controller_revalidation_trials_reject_reason"),
        ),
        sa.CheckConstraint(
            "stage IN ('EVIDENCE','CONFIRMATION')",
            name=op.f("ck_controller_revalidation_trials_stage"),
        ),
        sa.CheckConstraint(
            "verdict IN ('SUCCESS','FAILURE','TIMEOUT','INVALID_OUTPUT','VERIFICATION_UNAVAILABLE')",
            name=op.f("ck_controller_revalidation_trials_verdict"),
        ),
        sa.CheckConstraint(
            "(accepted AND reject_reason IS NULL) OR (NOT accepted AND reject_reason IS NOT NULL)",
            name=op.f("ck_controller_revalidation_trials_accepted_consistent"),
        ),
        sa.CheckConstraint(
            "tokens >= 0", name=op.f("ck_controller_revalidation_trials_tokens_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id", "version"],
            [
                "controller_policies.tenant_id",
                "controller_policies.policy_id",
                "controller_policies.version",
            ],
            name=op.f("fk_controller_revalidation_trials_tenant_id_policy_id_version"),
        ),
        sa.PrimaryKeyConstraint("trial_id", name=op.f("pk_controller_revalidation_trials")),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version",
            "cell_key",
            "episode",
            "cycle",
            "stage",
            "ordinal",
            name=op.f(
                "uq_controller_revalidation_trials_tenant_id_policy_id_version_cell_key_episode_cycle_stage_ordinal"
            ),
        ),
    )
    op.create_index(
        "ix_controller_revalidation_trials_version",
        "controller_revalidation_trials",
        ["tenant_id", "policy_id", "version"],
        unique=False,
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ee_reject_mutation()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM {table}) THEN
                RAISE EXCEPTION '{table} holds audit rows; not rewriting history';
              END IF;
            END $$
            """
        )
    op.drop_index("ix_controller_revalidation_trials_version", table_name=TRIALS)
    op.drop_table(TRIALS)
    op.drop_index("ix_controller_revalidation_transitions_version", table_name=TRANSITIONS)
    op.drop_table(TRANSITIONS)
