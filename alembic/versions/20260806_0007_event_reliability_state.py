"""Complete the processed_events state model for reliable delivery.

Revision ID: 20260806_0007
Revises: 20260805_0006

Upgrade notes:
- Adds reliable-delivery state columns to processed_events: attempt_count,
  next_attempt_at, lease_owner, lease_expires_at, result_summary,
  source_message_id, user_open_id, and updated_at.
- Existing rows are backfilled safely: rows that already reached processing
  (processing / succeeded / failed) get attempt_count=1, everything else 0;
  updated_at mirrors processed_at; source_message_id / user_open_id are
  denormalized from stored payloads where one exists. Legacy payload-less rows
  stay status=legacy_succeeded with NULL payload and remain non-replayable.
- Indexes serve the future worker's status/retry window queries, stale-lease
  scans, and operator lookups by source message or user.

Downgrade data loss:
- Drops the new columns and indexes. Retry/lease/result metadata and the
  denormalized lookup columns are discarded; payload_json and status remain.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260806_0007"
down_revision: str | None = "20260805_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processed_events",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processed_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("result_summary", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("user_open_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Backfill attempt_count and updated_at for pre-existing rows. A row that
    # already went through one sync processing attempt counts as one attempt.
    op.execute(
        sa.text(
            "UPDATE processed_events "
            "SET attempt_count = CASE WHEN status IN "
            "('processing', 'succeeded', 'failed') THEN 1 ELSE 0 END, "
            "updated_at = processed_at"
        )
    )
    # Denormalize lookup columns from stored payloads only (legacy rows keep NULL).
    op.execute(
        sa.text(
            "UPDATE processed_events "
            "SET source_message_id = payload_json->'event'->'message'->>'message_id', "
            "user_open_id = COALESCE("
            "payload_json->'event'->'sender'->'sender_id'->>'open_id', "
            "payload_json->'event'->'sender'->'sender_id'->>'user_id'"
            ") "
            "WHERE payload_json IS NOT NULL"
        )
    )

    op.create_index(
        "ix_events_status_next_attempt",
        "processed_events",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_events_lease_expires",
        "processed_events",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_events_source_message",
        "processed_events",
        ["source_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_events_user_open_id",
        "processed_events",
        ["user_open_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_events_user_open_id", table_name="processed_events")
    op.drop_index("ix_events_source_message", table_name="processed_events")
    op.drop_index("ix_events_lease_expires", table_name="processed_events")
    op.drop_index("ix_events_status_next_attempt", table_name="processed_events")
    op.drop_column("processed_events", "updated_at")
    op.drop_column("processed_events", "user_open_id")
    op.drop_column("processed_events", "source_message_id")
    op.drop_column("processed_events", "result_summary")
    op.drop_column("processed_events", "lease_expires_at")
    op.drop_column("processed_events", "lease_owner")
    op.drop_column("processed_events", "next_attempt_at")
    op.drop_column("processed_events", "attempt_count")
