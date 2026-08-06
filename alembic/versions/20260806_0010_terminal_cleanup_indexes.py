"""Add terminal retention cleanup indexes.

Revision ID: 20260806_0010
Revises: 20260806_0009

Upgrade notes:
- Adds status/time indexes used by P06d's bounded terminal cleanup scans.
- No rows are rewritten or deleted by this migration.

Downgrade notes:
- Drops only the four cleanup indexes; retained delivery and ledger data are
  unchanged.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260806_0010"
down_revision: str | None = "20260806_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_cleanup_processed",
        "processed_events",
        ["status", "processed_at"],
        unique=False,
    )
    op.create_index(
        "ix_events_cleanup_updated",
        "processed_events",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_cleanup_sent",
        "reply_outbox",
        ["status", "sent_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_cleanup_updated",
        "reply_outbox",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_cleanup_updated", table_name="reply_outbox")
    op.drop_index("ix_outbox_cleanup_sent", table_name="reply_outbox")
    op.drop_index("ix_events_cleanup_updated", table_name="processed_events")
    op.drop_index("ix_events_cleanup_processed", table_name="processed_events")
