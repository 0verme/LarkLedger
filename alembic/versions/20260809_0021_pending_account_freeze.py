"""Freeze the single-account target for non-transfer pending commands.

Revision ID: 20260809_0021
Revises: 20260809_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0021"
down_revision: str | None = "20260809_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TARGET_CONSTRAINT = (
    "(from_account_id IS NULL AND to_account_id IS NULL AND transfer_id IS NULL) OR "
    "(ledger_id IS NOT NULL AND from_account_id IS NOT NULL "
    "AND to_account_id IS NOT NULL AND transfer_id IS NOT NULL "
    "AND from_account_id <> to_account_id)"
)
_NEW_TARGET_CONSTRAINT = (
    "(account_id IS NULL AND from_account_id IS NULL AND to_account_id IS NULL "
    "AND transfer_id IS NULL) OR "
    "(account_id IS NOT NULL AND from_account_id IS NULL AND to_account_id IS NULL "
    "AND transfer_id IS NULL) OR "
    "(account_id IS NULL AND ledger_id IS NOT NULL AND from_account_id IS NOT NULL "
    "AND to_account_id IS NOT NULL AND transfer_id IS NOT NULL "
    "AND from_account_id <> to_account_id)"
)


def upgrade() -> None:
    op.add_column("pending_commands", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_pending_ledger_account",
        "pending_commands",
        "accounts",
        ["ledger_id", "account_id"],
        ["ledger_id", "id"],
        ondelete="RESTRICT",
    )
    # Existing rows have NULL account_id and either a transfer target or no
    # account target at all, so both branches of the old constraint remain
    # satisfied by the new constraint with account_id IS NULL.
    op.drop_constraint("ck_pending_transfer_target", "pending_commands", type_="check")
    op.create_check_constraint(
        "ck_pending_transfer_target",
        "pending_commands",
        _NEW_TARGET_CONSTRAINT,
    )


def downgrade() -> None:
    op.drop_constraint("ck_pending_transfer_target", "pending_commands", type_="check")
    op.create_check_constraint(
        "ck_pending_transfer_target",
        "pending_commands",
        _OLD_TARGET_CONSTRAINT,
    )
    op.drop_constraint("fk_pending_ledger_account", "pending_commands", type_="foreignkey")
    op.drop_column("pending_commands", "account_id")
