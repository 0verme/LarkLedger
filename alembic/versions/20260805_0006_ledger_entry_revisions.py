"""Add ledger_entry_revisions audit table.

Revision ID: 20260805_0006
Revises: 20260805_0005

Upgrade:
- Creates ledger_entry_revisions with before/after JSON snapshots.

Downgrade data loss:
- Drops the revisions table and all audit history. Ledger entries are unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260805_0006"
down_revision: str | None = "20260805_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ledger_entry_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_open_id", sa.String(length=128), nullable=False),
        sa.Column("short_id", sa.String(length=5), nullable=False),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_revisions_entry_created",
        "ledger_entry_revisions",
        ["entry_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_revisions_entry_created", table_name="ledger_entry_revisions")
    op.drop_table("ledger_entry_revisions")
