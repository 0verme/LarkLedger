"""Add ledger-scoped transfers, transfer audit, and frozen pending targets.

Revision ID: 20260809_0020
Revises: 20260809_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0020"
down_revision: str | None = "20260809_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("from_account_id", sa.Uuid(), nullable=False),
        sa.Column("to_account_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False, server_default="client"),
        sa.Column("source_message_id", sa.String(128), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_id", "id", name="uq_transfers_ledger_id"),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name="fk_transfers_ledger", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name="fk_transfers_actor", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "from_account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_transfers_ledger_from_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "to_account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_transfers_ledger_to_account",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "from_account_id <> to_account_id", name="ck_transfers_distinct_accounts"
        ),
        sa.CheckConstraint("amount > 0", name="ck_transfers_positive_amount"),
    )
    op.create_index("ix_transfers_ledger_occurred", "transfers", ["ledger_id", "occurred_at"])
    op.create_index(
        "ix_transfers_ledger_from", "transfers", ["ledger_id", "from_account_id", "occurred_at"]
    )
    op.create_index(
        "ix_transfers_ledger_to", "transfers", ["ledger_id", "to_account_id", "occurred_at"]
    )

    op.create_table(
        "transfer_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transfer_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "transfer_id"],
            ["transfers.ledger_id", "transfers.id"],
            name="fk_transfer_revisions_ledger_transfer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name="fk_transfer_revisions_actor", ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_transfer_revisions_transfer_created",
        "transfer_revisions",
        ["transfer_id", "created_at"],
    )

    op.add_column("pending_commands", sa.Column("from_account_id", sa.Uuid(), nullable=True))
    op.add_column("pending_commands", sa.Column("to_account_id", sa.Uuid(), nullable=True))
    op.add_column("pending_commands", sa.Column("transfer_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_pending_ledger_from_account",
        "pending_commands",
        "accounts",
        ["ledger_id", "from_account_id"],
        ["ledger_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_pending_ledger_to_account",
        "pending_commands",
        "accounts",
        ["ledger_id", "to_account_id"],
        ["ledger_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_pending_transfer_target",
        "pending_commands",
        "(from_account_id IS NULL AND to_account_id IS NULL AND transfer_id IS NULL) OR "
        "(ledger_id IS NOT NULL AND from_account_id IS NOT NULL "
        "AND to_account_id IS NOT NULL AND transfer_id IS NOT NULL "
        "AND from_account_id <> to_account_id)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pending_transfer_target", "pending_commands", type_="check")
    op.drop_constraint("fk_pending_ledger_to_account", "pending_commands", type_="foreignkey")
    op.drop_constraint("fk_pending_ledger_from_account", "pending_commands", type_="foreignkey")
    op.drop_column("pending_commands", "transfer_id")
    op.drop_column("pending_commands", "to_account_id")
    op.drop_column("pending_commands", "from_account_id")

    op.drop_index("ix_transfer_revisions_transfer_created", table_name="transfer_revisions")
    op.drop_table("transfer_revisions")
    op.drop_index("ix_transfers_ledger_to", table_name="transfers")
    op.drop_index("ix_transfers_ledger_from", table_name="transfers")
    op.drop_index("ix_transfers_ledger_occurred", table_name="transfers")
    op.drop_table("transfers")
