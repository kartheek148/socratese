"""recovery evidence semantics: outcome classes, outage triggers, provenance, gate reports (M6.4)

Revision ID: 0009
Revises: 0008

Widens the M6.3 revalidation tables; no new table. Trials gain two outcome classes (infrastructure
failure, incomplete execution), an explicit timeout origin and a provenance document. Transitions
gain the outage/evidence triggers and a gate report; a recovery under a policy that carries a
recovery block must be backed by a *passing* stored report (eligible, no failed gate, passed gates
and evidence listed, produced under the same configuration), which the database checks itself.
Rows written before this migration are unchanged and stay valid (their report is `{}` and their
policies carry no recovery block). The downgrade refuses while any row uses the new vocabulary
(history is not rewritten).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRANSITIONS = "controller_revalidation_transitions"
TRIALS = "controller_revalidation_trials"

OLD_TRIGGERS = (
    "CASE_OPENED", "INELIGIBLE", "READY", "TRIALS_STARTED", "CRITERIA_MET", "CRITERIA_NOT_MET",
    "CONFIRMED", "CONFIRMATION_FAILED", "VERIFICATION_UNAVAILABLE", "LINEAGE_VIOLATION",
    "CYCLES_EXHAUSTED", "TRIAL_CAP", "TOKEN_CAP", "LIFETIME_BUDGET", "TTL_EXPIRED", "CYCLE_EXPIRED",
)  # fmt: skip
NEW_TRIGGERS = (
    "SUSPENDED", "OUTAGE_PROBE", "RESUMED", "SUSPENSION_TIMEOUT", "EVIDENCE_EXPIRED", "GATE_FAILED",
)  # fmt: skip
OLD_VERDICTS = ("SUCCESS", "FAILURE", "TIMEOUT", "INVALID_OUTPUT", "VERIFICATION_UNAVAILABLE")
NEW_VERDICTS = ("INFRASTRUCTURE_FAILURE", "EXECUTION_INCOMPLETE")

TRIGGER_CK = op.f("ck_controller_revalidation_transitions_trigger")
VERDICT_CK = op.f("ck_controller_revalidation_trials_verdict")
GATES_OBJECT_CK = op.f("ck_controller_revalidation_transitions_gates_object")
RECOVERY_CK = op.f("ck_controller_revalidation_transitions_recovery_needs_passing_gates")
GATE_FAILED_CK = op.f("ck_controller_revalidation_transitions_gate_failed_not_eligible")
ORIGIN_CK = op.f("ck_controller_revalidation_trials_timeout_origin_matches_verdict")
PROVENANCE_CK = op.f("ck_controller_revalidation_trials_provenance_object")

RECOVERY_RULE = (
    "to_state <> 'RECOVERED' OR (config -> 'recovery') IS NULL OR "
    "CASE WHEN jsonb_typeof(gates -> 'failed_gates') = 'array' "
    "AND jsonb_typeof(gates -> 'passed_gates') = 'array' "
    "AND jsonb_typeof(gates -> 'evidence_ids') = 'array' "
    "THEN coalesce(gates ->> 'eligible', '') = 'true' "
    "AND coalesce(gates ->> 'policy_version', '') <> '' "
    "AND jsonb_array_length(gates -> 'failed_gates') = 0 "
    "AND jsonb_array_length(gates -> 'passed_gates') > 0 "
    "AND jsonb_array_length(gates -> 'evidence_ids') > 0 "
    "AND coalesce(gates ->> 'config_hash', '') = params_hash "
    "ELSE false END"
)
GATE_FAILED_RULE = "trigger <> 'GATE_FAILED' OR coalesce(gates ->> 'eligible', '') = 'false'"
ORIGIN_RULE = (
    "timeout_origin IS NULL OR (verdict = 'TIMEOUT' AND timeout_origin = 'CAPABILITY') "
    "OR (verdict = 'INFRASTRUCTURE_FAILURE' AND timeout_origin = 'INFRASTRUCTURE')"
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    empty = sa.text("'{}'::jsonb")
    op.add_column(
        TRANSITIONS,
        sa.Column("gates", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=empty),
    )  # fmt: skip
    op.drop_constraint(TRIGGER_CK, TRANSITIONS, type_="check")
    op.create_check_constraint(
        TRIGGER_CK, TRANSITIONS, _in("trigger", (*OLD_TRIGGERS, *NEW_TRIGGERS))
    )
    op.create_check_constraint(GATES_OBJECT_CK, TRANSITIONS, "jsonb_typeof(gates) = 'object'")
    op.create_check_constraint(RECOVERY_CK, TRANSITIONS, RECOVERY_RULE)
    op.create_check_constraint(GATE_FAILED_CK, TRANSITIONS, GATE_FAILED_RULE)

    op.add_column(TRIALS, sa.Column("timeout_origin", sa.Text(), nullable=True))
    op.add_column(
        TRIALS,
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=empty),
    )  # fmt: skip
    op.drop_constraint(VERDICT_CK, TRIALS, type_="check")
    op.create_check_constraint(VERDICT_CK, TRIALS, _in("verdict", (*OLD_VERDICTS, *NEW_VERDICTS)))
    op.create_check_constraint(ORIGIN_CK, TRIALS, ORIGIN_RULE)
    op.create_check_constraint(PROVENANCE_CK, TRIALS, "jsonb_typeof(provenance) = 'object'")


def downgrade() -> None:
    new_triggers = ",".join(f"'{t}'" for t in NEW_TRIGGERS)
    new_verdicts = ",".join(f"'{v}'" for v in NEW_VERDICTS)
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM {TRANSITIONS}
                     WHERE trigger IN ({new_triggers}) OR gates <> '{{}}'::jsonb) THEN
            RAISE EXCEPTION '{TRANSITIONS} holds M6.4 audit rows; not rewriting history';
          END IF;
          IF EXISTS (SELECT 1 FROM {TRIALS}
                     WHERE verdict IN ({new_verdicts}) OR timeout_origin IS NOT NULL
                        OR provenance <> '{{}}'::jsonb) THEN
            RAISE EXCEPTION '{TRIALS} holds M6.4 audit rows; not rewriting history';
          END IF;
        END $$
        """
    )
    op.drop_constraint(PROVENANCE_CK, TRIALS, type_="check")
    op.drop_constraint(ORIGIN_CK, TRIALS, type_="check")
    op.drop_constraint(VERDICT_CK, TRIALS, type_="check")
    op.create_check_constraint(VERDICT_CK, TRIALS, _in("verdict", OLD_VERDICTS))
    op.drop_column(TRIALS, "provenance")
    op.drop_column(TRIALS, "timeout_origin")

    op.drop_constraint(GATE_FAILED_CK, TRANSITIONS, type_="check")
    op.drop_constraint(RECOVERY_CK, TRANSITIONS, type_="check")
    op.drop_constraint(GATES_OBJECT_CK, TRANSITIONS, type_="check")
    op.drop_constraint(TRIGGER_CK, TRANSITIONS, type_="check")
    op.create_check_constraint(TRIGGER_CK, TRANSITIONS, _in("trigger", OLD_TRIGGERS))
    op.drop_column(TRANSITIONS, "gates")
