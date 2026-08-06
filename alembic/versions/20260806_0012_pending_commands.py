"""Add the pending_commands confirmation table (P07).

Revision ID: 20260806_0012
Revises: 20260806_0011

Upgrade notes:
- Adds ``pending_commands``: a frozen, high-risk command (image / voice / batch
  / likely-duplicate) awaiting user confirmation before it may write the ledger.
  ``payload_json`` holds the frozen ParsedCommand (no AI / media re-recognition
  on confirm); ``preview_json`` holds frozen user preview aggregates. The
  user-facing confirmation code is stored as ``CA83F2`` (display ``#C-A83F2``),
  user-unique and never reused. ``source_event_id`` has no foreign key so
  terminal event cleanup cannot cascade-delete an open confirmation.

Downgrade data loss:
- Drops pending confirmations (unexecuted and executed). Ledger entries written
  by confirmed commands are not affected; their audit trail is gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260806_0012"
down_revision: str | None = "20260806_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_commands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("confirmation_code", sa.String(length=6), nullable=False),
        sa.Column("user_open_id", sa.String(length=128), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
        sa.Column("transport", sa.String(length=16), nullable=False, server_default="feishu"),
        sa.Column("source_type", sa.String(length=16), nullable=False, server_default="text"),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("risk_reason", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "user_open_id", "confirmation_code", name="uq_pending_user_code"
        ),
        sa.UniqueConstraint("source_event_id", name="uq_pending_source_event"),
    )
    op.create_index(
        "ix_pending_status_expires",
        "pending_commands",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_pending_user_status",
        "pending_commands",
        ["user_open_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_pending_source_event",
        "pending_commands",
        ["source_event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pending_source_event", table_name="pending_commands")
    op.drop_index("ix_pending_user_status", table_name="pending_commands")
    op.drop_index("ix_pending_status_expires", table_name="pending_commands")
    op.drop_table("pending_commands")
