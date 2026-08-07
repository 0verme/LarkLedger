"""Deduplicate active visual pending confirmations by exact media fingerprint.

Revision ID: 20260807_0013
Revises: 20260806_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260807_0013"
down_revision: str | None = "20260806_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pending_commands",
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_pending_user_active_fingerprint",
        "pending_commands",
        ["user_open_id", "source_fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "source_fingerprint IS NOT NULL "
            "AND status IN ('pending', 'executing')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pending_user_active_fingerprint",
        table_name="pending_commands",
    )
    op.drop_column("pending_commands", "source_fingerprint")
