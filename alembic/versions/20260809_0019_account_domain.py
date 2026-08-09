"""Add ledger-scoped accounts and bind historical entries to defaults.

Revision ID: 20260809_0019
Revises: 20260809_0018
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0019"
down_revision: str | None = "20260809_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("subtype", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("opening_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_accounts_status"),
        sa.CheckConstraint("type IN ('cash', 'asset', 'liability')", name="ck_accounts_type"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_id", "id", name="uq_accounts_ledger_id"),
        sa.UniqueConstraint("ledger_id", "normalized_name", name="uq_accounts_ledger_name"),
    )
    op.create_index("ix_accounts_ledger_status", "accounts", ["ledger_id", "status", "created_at"])
    op.create_index(
        "uq_accounts_ledger_default",
        "accounts",
        ["ledger_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    bind = op.get_bind()
    ledgers = bind.execute(sa.text("SELECT id, currency FROM ledgers")).all()
    for ledger_id, currency in ledgers:
        bind.execute(
            sa.text(
                "INSERT INTO accounts "
                "(id, ledger_id, name, normalized_name, type, currency, "
                "opening_balance, status, is_default) "
                "VALUES (:id, :ledger_id, '默认账户', '默认账户', 'cash', "
                ":currency, 0, 'active', true)"
            ),
            {"id": uuid.uuid4(), "ledger_id": ledger_id, "currency": currency},
        )

    op.add_column("ledger_entries", sa.Column("account_id", sa.Uuid(), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE ledger_entries AS entry SET account_id = account.id "
            "FROM accounts AS account WHERE account.ledger_id = entry.ledger_id "
            "AND account.is_default = true"
        )
    )
    unresolved = bind.execute(
        sa.text("SELECT count(*) FROM ledger_entries WHERE account_id IS NULL")
    ).scalar_one()
    if int(unresolved) != 0:
        raise RuntimeError("account backfill is incomplete; refusing account migration")
    op.alter_column("ledger_entries", "account_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_entries_ledger_account",
        "ledger_entries",
        "accounts",
        ["ledger_id", "account_id"],
        ["ledger_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_entries_ledger_account", "ledger_entries", ["ledger_id", "account_id"])


def downgrade() -> None:
    op.drop_index("ix_entries_ledger_account", table_name="ledger_entries")
    op.drop_constraint("fk_entries_ledger_account", "ledger_entries", type_="foreignkey")
    op.drop_column("ledger_entries", "account_id")
    op.drop_index("uq_accounts_ledger_default", table_name="accounts")
    op.drop_index("ix_accounts_ledger_status", table_name="accounts")
    op.drop_table("accounts")
