"""Add bounded, ledger-scoped assistant conversations (P51).

Revision ID: 20260828_0029
Revises: 20260814_0028
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260828_0029"
down_revision: str | None = "20260814_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "resolved_query_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_assistant_conversations_status"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_conversations_actor_updated",
        "assistant_conversations",
        ["actor_user_id", "updated_at"],
    )
    op.create_index(
        "ix_assistant_conversations_ledger_updated",
        "assistant_conversations",
        ["ledger_id", "updated_at"],
    )
    op.create_table(
        "assistant_conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.String(2000), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')", name="ck_assistant_conversation_message_role"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["assistant_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_assistant_message_sequence"
        ),
    )
    op.create_index(
        "ix_assistant_messages_conversation_created",
        "assistant_conversation_messages",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_messages_conversation_created",
        table_name="assistant_conversation_messages",
    )
    op.drop_table("assistant_conversation_messages")
    op.drop_index(
        "ix_assistant_conversations_ledger_updated",
        table_name="assistant_conversations",
    )
    op.drop_index(
        "ix_assistant_conversations_actor_updated",
        table_name="assistant_conversations",
    )
    op.drop_table("assistant_conversations")
