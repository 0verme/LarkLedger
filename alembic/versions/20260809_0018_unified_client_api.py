"""Add bearer credentials, durable client idempotency and security audit.

Revision ID: 20260809_0018
Revises: 20260809_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0018"
down_revision: str | None = "20260809_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("current_ledger_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("scopes", sa.String(length=512), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("scopes <> ''", name="ck_client_credentials_scopes_not_empty"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_ledger_id"], ["ledgers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_client_credentials_token_digest"),
    )
    op.create_index(
        "ix_client_credentials_user_created", "client_credentials", ["user_id", "created_at"]
    )
    op.create_index("ix_client_credentials_expires", "client_credentials", ["expires_at"])

    op.create_table(
        "client_idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "operation",
            "ledger_id",
            "idempotency_key",
            name="uq_client_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_client_idempotency_expires", "client_idempotency_records", ["expires_at"]
    )
    op.create_index(
        "ix_client_idempotency_actor_created",
        "client_idempotency_records",
        ["actor_user_id", "created_at"],
    )

    op.create_table(
        "client_security_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_client_security_audits_actor_created",
        "client_security_audits",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_client_security_audits_action_created",
        "client_security_audits",
        ["action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_client_security_audits_action_created", table_name="client_security_audits")
    op.drop_index("ix_client_security_audits_actor_created", table_name="client_security_audits")
    op.drop_table("client_security_audits")
    op.drop_index(
        "ix_client_idempotency_actor_created", table_name="client_idempotency_records"
    )
    op.drop_index("ix_client_idempotency_expires", table_name="client_idempotency_records")
    op.drop_table("client_idempotency_records")
    op.drop_index("ix_client_credentials_expires", table_name="client_credentials")
    op.drop_index("ix_client_credentials_user_created", table_name="client_credentials")
    op.drop_table("client_credentials")
