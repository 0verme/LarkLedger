"""Add platform-independent users, identities, and ledger ownership.

Revision ID: 20260809_0015
Revises: 20260808_0014
"""

import os
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0015"
down_revision: str | None = "20260808_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ids(open_id: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    key = f"feishu:{open_id.strip()}"
    return (
        uuid.uuid5(uuid.NAMESPACE_URL, f"larkledger:user:{key}"),
        uuid.uuid5(uuid.NAMESPACE_URL, f"larkledger:identity:{key}"),
        uuid.uuid5(uuid.NAMESPACE_URL, f"larkledger:ledger:{key}:default"),
    )


def upgrade() -> None:
    currency = os.getenv("LARK_LEDGER_CURRENCY", "CNY").strip().upper()
    if len(currency) != 3:
        currency = "CNY"
    timezone = os.getenv("LARK_LEDGER_TIMEZONE", "Asia/Shanghai").strip()[:64]
    if not timezone:
        timezone = "Asia/Shanghai"
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ledgers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ledgers_owner_created", "ledgers", ["owner_user_id", "created_at"])
    op.create_index(
        "uq_ledgers_owner_default",
        "ledgers",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_table(
        "channel_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_subject_id", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "external_subject_id", name="uq_channel_identity_subject"),
    )
    op.create_index(
        "ix_channel_identities_user", "channel_identities", ["user_id", "created_at"]
    )

    op.add_column("ledger_entries", sa.Column("ledger_id", sa.Uuid(), nullable=True))
    op.add_column("category_budgets", sa.Column("ledger_id", sa.Uuid(), nullable=True))
    op.add_column("ledger_entry_revisions", sa.Column("ledger_id", sa.Uuid(), nullable=True))
    op.add_column("ledger_entry_revisions", sa.Column("actor_user_id", sa.Uuid(), nullable=True))
    op.add_column("pending_commands", sa.Column("actor_user_id", sa.Uuid(), nullable=True))
    op.add_column("pending_commands", sa.Column("ledger_id", sa.Uuid(), nullable=True))
    op.add_column("dashboard_sessions", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.add_column("dashboard_sessions", sa.Column("ledger_id", sa.Uuid(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT user_open_id FROM ("
            "SELECT user_open_id FROM ledger_entries UNION ALL "
            "SELECT user_open_id FROM category_budgets UNION ALL "
            "SELECT user_open_id FROM ledger_entry_revisions UNION ALL "
            "SELECT user_open_id FROM pending_commands UNION ALL "
            "SELECT user_open_id FROM dashboard_sessions"
            ") subjects WHERE user_open_id IS NOT NULL AND user_open_id <> ''"
        )
    ).scalars()
    for open_id in rows:
        subject = str(open_id)
        user_id, identity_id, ledger_id = _ids(subject)
        display_name = bind.execute(
            sa.text(
                "SELECT display_name FROM dashboard_sessions "
                "WHERE user_open_id = :open_id AND display_name <> '' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"open_id": subject},
        ).scalar_one_or_none()
        bind.execute(
            sa.text(
                "INSERT INTO users (id, display_name, status) "
                "VALUES (:id, :display_name, 'active')"
            ),
            {"id": user_id, "display_name": str(display_name or "")},
        )
        bind.execute(
            sa.text(
                "INSERT INTO ledgers "
                "(id, owner_user_id, name, kind, currency, timezone, is_default) "
                "VALUES (:id, :user_id, :name, 'personal', :currency, :timezone, true)"
            ),
            {
                "id": ledger_id,
                "user_id": user_id,
                "name": "我的账本",
                "currency": currency,
                "timezone": timezone,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO channel_identities "
                "(id, user_id, channel, external_subject_id) "
                "VALUES (:id, :user_id, 'feishu', :subject)"
            ),
            {"id": identity_id, "user_id": user_id, "subject": subject},
        )
        for table in ("ledger_entries", "category_budgets", "ledger_entry_revisions"):
            bind.execute(
                sa.text(f"UPDATE {table} SET ledger_id = :ledger_id WHERE user_open_id = :subject"),
                {"ledger_id": ledger_id, "subject": subject},
            )
        bind.execute(
            sa.text(
                "UPDATE ledger_entry_revisions SET actor_user_id = :user_id "
                "WHERE user_open_id = :subject"
            ),
            {"user_id": user_id, "subject": subject},
        )
        bind.execute(
            sa.text(
                "UPDATE pending_commands SET actor_user_id = :user_id, ledger_id = :ledger_id "
                "WHERE user_open_id = :subject"
            ),
            {"user_id": user_id, "ledger_id": ledger_id, "subject": subject},
        )
        bind.execute(
            sa.text(
                "UPDATE dashboard_sessions SET user_id = :user_id, ledger_id = :ledger_id "
                "WHERE user_open_id = :subject"
            ),
            {"user_id": user_id, "ledger_id": ledger_id, "subject": subject},
        )

    # Keep the compatibility columns nullable during the expand release. Every
    # row that existed at migration time is backfilled above and new application
    # writes populate them. A later contract migration can make them NOT NULL
    # after operators have had a full release window to verify the backfill.
    ledger_tables = (
        "ledger_entries",
        "category_budgets",
        "ledger_entry_revisions",
        "pending_commands",
        "dashboard_sessions",
    )
    for table in ledger_tables:
        op.create_foreign_key(
            f"fk_{table}_ledger_id", table, "ledgers", ["ledger_id"], ["id"], ondelete="RESTRICT"
        )
    op.create_foreign_key(
        "fk_revisions_actor_user_id",
        "ledger_entry_revisions",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_pending_actor_user_id",
        "pending_commands",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_dashboard_sessions_user_id",
        "dashboard_sessions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_entries_ledger_short_id", "ledger_entries", ["ledger_id", "short_id"]
    )
    op.create_unique_constraint(
        "uq_budgets_ledger_category", "category_budgets", ["ledger_id", "category"]
    )
    op.create_index(
        "ix_entries_ledger_occurred", "ledger_entries", ["ledger_id", "occurred_at"]
    )
    op.create_index(
        "ix_entries_ledger_category", "ledger_entries", ["ledger_id", "category"]
    )


def downgrade() -> None:
    op.drop_index("ix_entries_ledger_category", table_name="ledger_entries")
    op.drop_index("ix_entries_ledger_occurred", table_name="ledger_entries")
    op.drop_constraint("uq_budgets_ledger_category", "category_budgets", type_="unique")
    op.drop_constraint("uq_entries_ledger_short_id", "ledger_entries", type_="unique")
    op.drop_constraint("fk_dashboard_sessions_user_id", "dashboard_sessions", type_="foreignkey")
    op.drop_constraint("fk_pending_actor_user_id", "pending_commands", type_="foreignkey")
    op.drop_constraint("fk_revisions_actor_user_id", "ledger_entry_revisions", type_="foreignkey")
    ledger_tables = (
        "dashboard_sessions",
        "pending_commands",
        "ledger_entry_revisions",
        "category_budgets",
        "ledger_entries",
    )
    for table in ledger_tables:
        op.drop_constraint(f"fk_{table}_ledger_id", table, type_="foreignkey")
    op.drop_column("dashboard_sessions", "ledger_id")
    op.drop_column("dashboard_sessions", "user_id")
    op.drop_column("pending_commands", "ledger_id")
    op.drop_column("pending_commands", "actor_user_id")
    op.drop_column("ledger_entry_revisions", "actor_user_id")
    op.drop_column("ledger_entry_revisions", "ledger_id")
    op.drop_column("category_budgets", "ledger_id")
    op.drop_column("ledger_entries", "ledger_id")
    op.drop_index("ix_channel_identities_user", table_name="channel_identities")
    op.drop_table("channel_identities")
    op.drop_index("uq_ledgers_owner_default", table_name="ledgers")
    op.drop_index("ix_ledgers_owner_created", table_name="ledgers")
    op.drop_table("ledgers")
    op.drop_table("users")
