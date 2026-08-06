"""Add guarded manual event replay state and audit records.

Revision ID: 20260806_0011
Revises: 20260806_0010

Upgrade notes:
- Adds an append-only event_replay_audits table without a payload copy.
- Adds manual_replay_count, replay_safety_version, and business_committed_at to
  processed_events.
- Historical events intentionally keep replay_safety_version NULL because the
  migration cannot prove that older business writes used the atomic outbox.

Downgrade data loss:
- Drops replay audits and the two replay metadata columns. Ledger, event
  payload, outbox, and delivery data are unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260806_0011"
down_revision: str | None = "20260806_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processed_events",
        sa.Column(
            "manual_replay_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "processed_events",
        sa.Column("replay_safety_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("business_committed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing outbox rows prove that their business transaction committed.
    # Preserve that proof on the event before retention may later delete the
    # outbox. Events without an outbox remain unmarked and unguessed.
    op.execute(
        sa.text(
            "UPDATE processed_events AS event "
            "SET business_committed_at = COALESCE(event.updated_at, event.processed_at) "
            "WHERE EXISTS ("
            "SELECT 1 FROM reply_outbox AS outbox WHERE outbox.event_id = event.event_id"
            ")"
        )
    )
    op.create_table(
        "event_replay_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("operator", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("previous_attempt_count", sa.Integer(), nullable=True),
        sa.Column("replay_number", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("resulting_status", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_event_replay_audits_event_created",
        "event_replay_audits",
        ["event_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_replay_audits_event_created",
        table_name="event_replay_audits",
    )
    op.drop_table("event_replay_audits")
    op.drop_column("processed_events", "business_committed_at")
    op.drop_column("processed_events", "replay_safety_version")
    op.drop_column("processed_events", "manual_replay_count")
