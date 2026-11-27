"""evidence drift monitor: append-only state-transition audit (M6.2)

Revision ID: 0007
Revises: 0006

One new table; nothing existing changes. Rows are audit evidence: the downgrade refuses to drop
the table while it holds rows (history is not rewritten).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = "'NORMAL','SUSPECT','DRIFTED','QUARANTINED','RECOVERING'"
TABLE = "controller_drift_transitions"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("transition_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("cell_key", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("measurements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("n_window", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("detector_version", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"from_state IN ({STATES})", name=op.f("ck_controller_drift_transitions_from_state")
        ),
        sa.CheckConstraint(
            f"to_state IN ({STATES})", name=op.f("ck_controller_drift_transitions_to_state")
        ),
        sa.CheckConstraint(
            "mode IN ('SHADOW','ENFORCE')", name=op.f("ck_controller_drift_transitions_mode")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_controller_drift_transitions_seq_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id", "version"],
            [
                "controller_policies.tenant_id",
                "controller_policies.policy_id",
                "controller_policies.version",
            ],
            name=op.f("fk_controller_drift_transitions_tenant_id_policy_id_version"),
        ),
        sa.PrimaryKeyConstraint("transition_id", name=op.f("pk_controller_drift_transitions")),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version",
            "cell_key",
            "seq",
            name=op.f("uq_controller_drift_transitions_tenant_id_policy_id_version_cell_key_seq"),
        ),
    )
    op.create_index(
        "ix_controller_drift_transitions_version",
        TABLE,
        ["tenant_id", "policy_id", "version"],
        unique=False,
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_append_only BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION ee_reject_mutation()"
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM {TABLE}) THEN
            RAISE EXCEPTION '{TABLE} holds audit rows; not rewriting history';
          END IF;
        END $$
        """
    )
    op.drop_index("ix_controller_drift_transitions_version", table_name=TABLE)
    op.drop_table(TABLE)
