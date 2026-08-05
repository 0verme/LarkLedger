"""Persist claim-time event payloads for future replay.

Revision ID: 20260805_0004
Revises: 20260803_0003

Upgrade notes:
- New columns on processed_events store a versioned JSON envelope for new claims.
- Existing rows are marked status=legacy_succeeded with NULL payload_json and are
  intentionally not replayable.

Downgrade data loss:
- Drops payload_json, payload_version, transport, status, received_at, and
  last_error_code. Any persisted envelopes and processing status are discarded.
- event_id and processed_at are preserved so claim-first de-duplication history
  remains, but recovery metadata is gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260805_0004"
down_revision: str | None = "20260803_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("processed_events", sa.Column("payload_json", sa.JSON(), nullable=True))
    op.add_column("processed_events", sa.Column("payload_version", sa.Integer(), nullable=True))
    op.add_column("processed_events", sa.Column("transport", sa.String(length=16), nullable=True))
    op.add_column(
        "processed_events",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_succeeded",
        ),
    )
    op.add_column(
        "processed_events",
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
    )
    # Historical claims: keep de-duplication keys, mark non-replayable.
    op.execute(
        sa.text(
            "UPDATE processed_events "
            "SET status = 'legacy_succeeded', "
            "payload_json = NULL, "
            "payload_version = NULL, "
            "transport = NULL, "
            "received_at = NULL, "
            "last_error_code = NULL"
        )
    )
    # New application rows always set status explicitly.
    op.alter_column("processed_events", "status", server_default=None)


def downgrade() -> None:
    # Explicit data-loss boundary for operators rolling back this revision.
    op.drop_column("processed_events", "last_error_code")
    op.drop_column("processed_events", "received_at")
    op.drop_column("processed_events", "status")
    op.drop_column("processed_events", "transport")
    op.drop_column("processed_events", "payload_version")
    op.drop_column("processed_events", "payload_json")
