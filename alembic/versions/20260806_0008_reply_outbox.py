"""Add the transactional reply outbox (P06a).

Revision ID: 20260806_0008
Revises: 20260806_0007

Upgrade notes:
- Adds ``reply_outbox``: a durable, self-contained Feishu reply intent written
  in the same transaction as the ledger change it confirms. One event may
  produce several replies (e.g. a CSV export sends a file message then a text
  confirmation), so ``(event_id, reply_type)`` is unique and ``sequence``
  orders them. ``event_id`` is nullable only for out-of-band direct calls; the
  production processor always links the row to its source event.
- ``payload_blob`` holds file / report-image bytes so a later worker can
  deliver after a container restart without a temporary file. ``size`` and
  ``sha256`` are recorded in ``payload_json`` for integrity checks.
- ``status`` values come from the ``ReplyStatus`` enum. ``sending`` and
  ``dead`` are reserved for the P06b background worker; P06a writes only
  ``pending`` / ``sent`` / ``failed``.
- Indexes: ``(status, next_attempt_at)`` and ``lease_expires_at`` are the
  P06b worker's future claim window and stale-lease scans; the unique
  ``(event_id, reply_type)`` constraint backs idempotency and the recovery
  pre-check.

Downgrade data loss:
- Drops the ``reply_outbox`` table and all undelivered reply intents (pending /
  failed rows are discarded; their replies would have to be regenerated).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260806_0008"
down_revision: str | None = "20260806_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reply_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_id",
            sa.String(length=128),
            sa.ForeignKey("processed_events.event_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("reply_type", sa.String(length=16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transport", sa.String(length=16), nullable=False, server_default="feishu"),
        sa.Column("payload_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_blob", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("result_summary", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "reply_type", name="uq_outbox_event_type"),
    )
    op.create_index(
        "ix_outbox_status_next_attempt",
        "reply_outbox",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_lease_expires",
        "reply_outbox",
        ["lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_lease_expires", table_name="reply_outbox")
    op.drop_index("ix_outbox_status_next_attempt", table_name="reply_outbox")
    op.drop_table("reply_outbox")
