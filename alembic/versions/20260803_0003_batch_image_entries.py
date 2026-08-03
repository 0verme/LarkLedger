"""Support multiple ledger entries from one source message."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260803_0003"
down_revision: str | None = "20260803_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column("source_item_index", sa.SmallInteger(), nullable=True),
    )
    op.execute(
        "UPDATE ledger_entries SET source_item_index = 0 "
        "WHERE source_message_id IS NOT NULL"
    )
    op.drop_constraint(
        "ledger_entries_source_message_id_key",
        "ledger_entries",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_entries_source_item",
        "ledger_entries",
        ["source_message_id", "source_item_index"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_entries_source_item", "ledger_entries", type_="unique")
    # Older schemas cannot represent several entries with the same source message.
    # Preserve every ledger row but drop source attribution from additional items.
    op.execute(
        "UPDATE ledger_entries SET source_message_id = NULL "
        "WHERE source_item_index IS NOT NULL AND source_item_index <> 0"
    )
    op.drop_column("ledger_entries", "source_item_index")
    op.create_unique_constraint(
        "ledger_entries_source_message_id_key",
        "ledger_entries",
        ["source_message_id"],
    )
