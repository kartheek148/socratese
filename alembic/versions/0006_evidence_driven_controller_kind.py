"""allow the evidence-driven controller kind (M6.1)

Revision ID: 0006
Revises: 0005

Only the CHECK on `controller_policies.kind` changes. Stored definitions are JSON: the paired
prior and the evidence parameters are optional fields, so every existing row (and its
definition hash) is untouched.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_controller_policies_kind"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "controller_policies", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "controller_policies", "kind IN ('COST_AWARE','BANDIT','EVIDENCE_DRIVEN')"
    )


def downgrade() -> None:
    # History is never rewritten: refuse rather than delete evidence-driven versions.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM controller_policies WHERE kind = 'EVIDENCE_DRIVEN') THEN
            RAISE EXCEPTION 'controller_policies has EVIDENCE_DRIVEN versions; not rewriting history';
          END IF;
        END $$
        """
    )
    op.drop_constraint(op.f(CONSTRAINT), "controller_policies", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "controller_policies", "kind IN ('COST_AWARE','BANDIT')"
    )
