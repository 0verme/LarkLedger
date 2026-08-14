"""Append-only dead-letter action audit (P44).

Adds ``dead_letter_actions`` — the unified operator audit for dead-letter
``replay`` / ``resolve`` operations across every backlog source (``events`` /
``outbox`` / ``pending_commands``). One row per human action; payloads,
financial content and credentials are never stored. ``target_id`` has no
foreign key so terminal cleanup of source rows cannot erase the audit.

Indexes ride the existing lookup patterns: ``(source, target_id, created_at)``
for per-item history and ``created_at`` for the recent-actions query.

Upgrade is purely additive and backward-safe; downgrade drops the table (the
source tables are untouched in both directions).

Revision ID: 20260814_0028
Revises: 20260814_0027
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260814_0028"
down_revision: str | None = "20260814_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("operator", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.Column("before_status", sa.String(32), nullable=True),
        sa.Column("after_status", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dead_letter_actions_source_target",
        "dead_letter_actions",
        ["source", "target_id", "created_at"],
    )
    op.create_index(
        "ix_dead_letter_actions_created",
        "dead_letter_actions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dead_letter_actions_created", table_name="dead_letter_actions")
    op.drop_index("ix_dead_letter_actions_source_target", table_name="dead_letter_actions")
    op.drop_table("dead_letter_actions")
