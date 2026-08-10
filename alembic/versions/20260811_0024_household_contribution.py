"""Add household contribution: payer attribution, member aliases (P30).

New columns:

* ``ledger_entries.created_by_user_id`` / ``paid_by_user_id`` — who issued the
  booking action vs who actually paid. Backfilled losslessly from the entry's
  ``user_open_id`` via ``channel_identities`` (the creator's internal user);
  rows whose creator cannot be resolved keep NULL and are handled as "unknown
  payer" by reads. New entries always set both through the service layer.
* ``recurring_rules.paid_by_user_id`` — who pays each occurrence. Backfilled
  to ``creator_user_id`` (always present) and enforced NOT NULL, so existing
  rules behave exactly as before.
* ``pending_commands.paid_by_user_id`` — the frozen payer for recurring-
  generated pendings, so confirming never changes the payer.
* ``household_members.alias`` — an optional per-household payer alias
  (``老婆`` / ``爸爸``) used by deterministic payer resolution; unique per
  household (NULLs are distinct, so unset members are unaffected).

Revision ID: 20260811_0024
Revises: 20260810_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0024"
down_revision: str | None = "20260810_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- ledger_entries: created_by / paid_by ---------------------------------
    op.add_column("ledger_entries", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("ledger_entries", sa.Column("paid_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_entries_created_by_user",
        "ledger_entries",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_entries_paid_by_user",
        "ledger_entries",
        "users",
        ["paid_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Lossless backfill: the entry's user_open_id maps to its creator's internal
    # user through the feishu channel identity. At most one identity matches per
    # subject, so created_by == paid_by for all historical rows.
    op.execute(
        sa.text(
            "UPDATE ledger_entries SET "
            "created_by_user_id = ci.user_id, paid_by_user_id = ci.user_id "
            "FROM channel_identities ci "
            "WHERE ci.channel = 'feishu' "
            "AND ci.external_subject_id = ledger_entries.user_open_id"
        )
    )
    op.create_index(
        "ix_entries_ledger_paid_by",
        "ledger_entries",
        ["ledger_id", "paid_by_user_id"],
    )

    # -- recurring_rules: paid_by ----------------------------------------------
    op.add_column("recurring_rules", sa.Column("paid_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_recurring_rules_paid_by_user",
        "recurring_rules",
        "users",
        ["paid_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        sa.text("UPDATE recurring_rules SET paid_by_user_id = creator_user_id")
    )
    op.alter_column(
        "recurring_rules",
        "paid_by_user_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )

    # -- pending_commands: frozen payer -----------------------------------------
    op.add_column("pending_commands", sa.Column("paid_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_pending_commands_paid_by_user",
        "pending_commands",
        "users",
        ["paid_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # -- household_members: alias ------------------------------------------------
    op.add_column("household_members", sa.Column("alias", sa.String(length=32), nullable=True))
    op.create_unique_constraint(
        "uq_household_members_alias",
        "household_members",
        ["household_id", "alias"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_household_members_alias", "household_members", type_="unique")
    op.drop_column("household_members", "alias")

    op.drop_constraint("fk_pending_commands_paid_by_user", "pending_commands", type_="foreignkey")
    op.drop_column("pending_commands", "paid_by_user_id")

    op.drop_constraint("fk_recurring_rules_paid_by_user", "recurring_rules", type_="foreignkey")
    op.drop_column("recurring_rules", "paid_by_user_id")

    op.drop_index("ix_entries_ledger_paid_by", table_name="ledger_entries")
    op.drop_constraint("fk_entries_paid_by_user", "ledger_entries", type_="foreignkey")
    op.drop_constraint("fk_entries_created_by_user", "ledger_entries", type_="foreignkey")
    op.drop_column("ledger_entries", "paid_by_user_id")
    op.drop_column("ledger_entries", "created_by_user_id")
