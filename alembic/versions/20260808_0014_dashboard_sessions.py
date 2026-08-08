"""Add revocable Web Dashboard sessions.

Revision ID: 20260808_0014
Revises: 20260807_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0014"
down_revision: str | None = "20260807_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dashboard_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("user_open_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("avatar_url", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_dashboard_sessions_token_hash"),
    )
    op.create_index("ix_dashboard_sessions_expires", "dashboard_sessions", ["expires_at"])
    op.create_index(
        "ix_dashboard_sessions_user",
        "dashboard_sessions",
        ["user_open_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_sessions_user", table_name="dashboard_sessions")
    op.drop_index("ix_dashboard_sessions_expires", table_name="dashboard_sessions")
    op.drop_table("dashboard_sessions")
