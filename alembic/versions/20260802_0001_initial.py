"""Create ledger and event tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260802_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    direction = postgresql.ENUM(
        "EXPENSE", "INCOME", name="entry_direction", create_type=False
    )
    direction.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_open_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("direction", direction, nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_message_id"),
    )
    op.create_index("ix_entries_user_category", "ledger_entries", ["user_open_id", "category"])
    op.create_index("ix_entries_user_occurred", "ledger_entries", ["user_open_id", "occurred_at"])
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_index("ix_entries_user_occurred", table_name="ledger_entries")
    op.drop_index("ix_entries_user_category", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    sa.Enum(name="entry_direction").drop(op.get_bind(), checkfirst=True)
