"""adaptive controller policies, rollout evidence and shadow decisions

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = "'CANDIDATE','SHADOW','CANARY','ACTIVE','SUPERSEDED','REJECTED','ROLLED_BACK'"


def upgrade() -> None:
    # A shadow decision is recorded but never executed.
    op.add_column(
        "policy_decisions",
        sa.Column("acted", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_table(
        "controller_policies",
        sa.Column("version_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"state IN ({STATES})", name=op.f("ck_controller_policies_state")),
        sa.CheckConstraint(
            "kind IN ('COST_AWARE','BANDIT')", name=op.f("ck_controller_policies_kind")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_controller_policies_version_positive")),
        sa.PrimaryKeyConstraint("version_id", name=op.f("pk_controller_policies")),
        sa.UniqueConstraint(
            "tenant_id",
            "policy_id",
            "version",
            name=op.f("uq_controller_policies_tenant_id_policy_id_version"),
        ),
    )
    op.create_index(
        "ix_controller_policies_tenant_id_policy_id_state",
        "controller_policies",
        ["tenant_id", "policy_id", "state"],
        unique=False,
    )
    for name in ("active", "canary", "shadow"):
        op.create_index(
            f"uq_controller_policies_one_{name}",
            "controller_policies",
            ["tenant_id", "policy_id"],
            unique=True,
            postgresql_where=sa.text(f"state = '{name.upper()}'"),
        )
    op.create_table(
        "controller_policy_evaluations",
        sa.Column("evaluation_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("definition_hash", sa.Text(), nullable=False),
        sa.Column("gate_hash", sa.Text(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('OFFLINE','SHADOW','CANARY')",
            name=op.f("ck_controller_policy_evaluations_stage"),
        ),
        sa.CheckConstraint(
            "verdict IN ('PASS','FAIL','INCONCLUSIVE')",
            name=op.f("ck_controller_policy_evaluations_verdict"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id", "version"],
            [
                "controller_policies.tenant_id",
                "controller_policies.policy_id",
                "controller_policies.version",
            ],
            name=op.f("fk_controller_policy_evaluations_tenant_id_policy_id_version"),
        ),
        sa.PrimaryKeyConstraint("evaluation_id", name=op.f("pk_controller_policy_evaluations")),
    )
    op.create_index(
        "ix_controller_policy_evaluations_version",
        "controller_policy_evaluations",
        ["tenant_id", "policy_id", "version"],
        unique=False,
    )
    op.create_table(
        "controller_policy_transitions",
        sa.Column("transition_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(reason) <= 500",
            name=op.f("ck_controller_policy_transitions_reason_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_id", "version"],
            [
                "controller_policies.tenant_id",
                "controller_policies.policy_id",
                "controller_policies.version",
            ],
            name=op.f("fk_controller_policy_transitions_tenant_id_policy_id_version"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["controller_policy_evaluations.evaluation_id"],
            name=op.f("fk_controller_policy_transitions_evaluation_id"),
        ),
        sa.PrimaryKeyConstraint("transition_id", name=op.f("pk_controller_policy_transitions")),
    )
    op.create_index(
        "ix_controller_policy_transitions_version",
        "controller_policy_transitions",
        ["tenant_id", "policy_id", "version"],
        unique=False,
    )
    for table in ("controller_policy_evaluations", "controller_policy_transitions"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ee_reject_mutation()"
        )
    # Only `state` may change, only along the legal graph, and every forward step needs a PASS
    # evaluation of the previous stage for exactly this definition. SUPERSEDED -> ACTIVE is the
    # restore step of a rollback and needs no new evidence (the version was ACTIVE before).
    op.execute(
        """
        CREATE FUNCTION ee_controller_policy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE need_stage text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'controller_policies rows cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (to_jsonb(NEW) - 'state') IS DISTINCT FROM (to_jsonb(OLD) - 'state') THEN
                RAISE EXCEPTION 'controller_policies are immutable except for state' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.state <> OLD.state THEN
                IF NOT ((OLD.state, NEW.state) IN (
                    ('CANDIDATE','SHADOW'), ('CANDIDATE','REJECTED'),
                    ('SHADOW','CANARY'), ('SHADOW','REJECTED'), ('SHADOW','ROLLED_BACK'),
                    ('CANARY','ACTIVE'), ('CANARY','ROLLED_BACK'),
                    ('ACTIVE','SUPERSEDED'), ('ACTIVE','ROLLED_BACK'),
                    ('SUPERSEDED','ACTIVE')
                )) THEN
                    RAISE EXCEPTION 'illegal controller policy transition % -> %', OLD.state, NEW.state USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                need_stage := CASE
                    WHEN OLD.state = 'CANDIDATE' AND NEW.state = 'SHADOW' THEN 'OFFLINE'
                    WHEN OLD.state = 'SHADOW' AND NEW.state = 'CANARY' THEN 'SHADOW'
                    WHEN OLD.state = 'CANARY' AND NEW.state = 'ACTIVE' THEN 'CANARY'
                    ELSE NULL END;
                IF need_stage IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM controller_policy_evaluations e
                    WHERE e.tenant_id = NEW.tenant_id AND e.policy_id = NEW.policy_id
                      AND e.version = NEW.version AND e.stage = need_stage
                      AND e.verdict = 'PASS' AND e.definition_hash = NEW.definition_hash
                ) THEN
                    RAISE EXCEPTION 'a controller policy cannot advance to % without a passing % evaluation of this definition', NEW.state, need_stage USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER controller_policies_guard BEFORE UPDATE OR DELETE ON controller_policies "
        "FOR EACH ROW EXECUTE FUNCTION ee_controller_policy_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER controller_policies_guard ON controller_policies")
    op.execute("DROP FUNCTION ee_controller_policy_guard()")
    for table in ("controller_policy_evaluations", "controller_policy_transitions"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_index(
        "ix_controller_policy_transitions_version", table_name="controller_policy_transitions"
    )
    op.drop_table("controller_policy_transitions")
    op.drop_index(
        "ix_controller_policy_evaluations_version", table_name="controller_policy_evaluations"
    )
    op.drop_table("controller_policy_evaluations")
    for name in ("shadow", "canary", "active"):
        op.drop_index(
            f"uq_controller_policies_one_{name}",
            table_name="controller_policies",
            postgresql_where=sa.text(f"state = '{name.upper()}'"),
        )
    op.drop_index(
        "ix_controller_policies_tenant_id_policy_id_state", table_name="controller_policies"
    )
    op.drop_table("controller_policies")
    op.drop_column("policy_decisions", "acted")
