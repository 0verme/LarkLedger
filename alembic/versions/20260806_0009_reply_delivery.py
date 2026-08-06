"""Add reply delivery metadata and sequence index (P06b).

Revision ID: 20260806_0009
Revises: 20260806_0008

Upgrade notes:
- Adds ``remote_message_id``: the Feishu ``message_id`` of the delivered reply
  (returned by the reply API), recorded on ``sent`` for auditability.
- Adds ``remote_file_key`` / ``remote_image_key``: resource keys obtained from
  the Feishu file / image upload APIs and persisted right after upload, so a
  retry after a message-send failure reuses the already-uploaded resource
  instead of uploading again.
- Adds ``ix_outbox_event_sequence`` on ``(event_id, sequence)``: backs the
  Reply Worker's ordering guarantee (a later reply in the same event waits for
  its earlier siblings) via the ``NOT EXISTS`` claim predicate. The existing
  ``(event_id, reply_type)`` unique constraint is unchanged — it already
  supports every real reply combination (one text, or one file + one text, or
  one card per event).

All new columns are nullable, so historical P06a rows (``pending`` / ``sent`` /
``failed``) are untouched. Delivery metadata for already-``sent`` rows is not
backfilled.

Downgrade data loss:
- Drops the three delivery-metadata columns; recorded remote message IDs and
  uploaded resource keys are lost (a later delivery would re-upload the blob,
  which is safe, and the sent-message audit trail is reduced).
- Drops the ``(event_id, sequence)`` index; the ordering claim still works but
  without a dedicated supporting index.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260806_0009"
down_revision: str | None = "20260806_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reply_outbox",
        sa.Column("remote_message_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "reply_outbox",
        sa.Column("remote_file_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "reply_outbox",
        sa.Column("remote_image_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_outbox_event_sequence",
        "reply_outbox",
        ["event_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_event_sequence", table_name="reply_outbox")
    op.drop_column("reply_outbox", "remote_image_key")
    op.drop_column("reply_outbox", "remote_file_key")
    op.drop_column("reply_outbox", "remote_message_id")
