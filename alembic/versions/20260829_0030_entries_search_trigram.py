"""Add trigram indexes for Entries substring search.

Revision ID: 20260829_0030
Revises: 20260828_0029
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0030"
down_revision: str | None = "20260828_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, column in (
        ("ix_entries_note_trgm", "note"),
        ("ix_entries_category_trgm", "category"),
        ("ix_entries_short_id_trgm", "short_id"),
    ):
        op.create_index(
            name,
            "ledger_entries",
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )


def downgrade() -> None:
    for name in (
        "ix_entries_short_id_trgm",
        "ix_entries_category_trgm",
        "ix_entries_note_trgm",
    ):
        op.drop_index(name, table_name="ledger_entries")
