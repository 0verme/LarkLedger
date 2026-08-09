"""Add household spaces, memberships, invitations and shared ledgers.

Revision ID: 20260809_0017
Revises: 20260809_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0017"
down_revision: str | None = "20260809_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", "normalized_name", name="uq_households_owner_name"),
    )
    op.create_index("ix_households_owner_created", "households", ["owner_user_id", "created_at"])

    op.create_table(
        "household_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_household_members_role"),
        sa.CheckConstraint(
            "status IN ('active', 'left', 'removed')", name="ck_household_members_status"
        ),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", "user_id", name="uq_household_members_user"),
    )
    op.create_index(
        "uq_household_members_active_owner",
        "household_members",
        ["household_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner' AND status = 'active'"),
    )
    op.create_index("ix_household_members_user_status", "household_members", ["user_id", "status"])

    op.create_table(
        "household_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("inviter_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_channel_identity_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired')",
            name="ck_household_invitations_status",
        ),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inviter_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_channel_identity_id"], ["channel_identities.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_household_invitations_public_id"),
    )
    op.create_index(
        "uq_household_invitations_active_target",
        "household_invitations",
        ["household_id", "target_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_household_invitations_target_status",
        "household_invitations",
        ["target_user_id", "status"],
    )
    op.create_index(
        "ix_household_invitations_expires",
        "household_invitations",
        ["status", "expires_at"],
    )

    op.alter_column("ledgers", "owner_user_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("ledgers", sa.Column("household_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_ledgers_household_id",
        "ledgers",
        "households",
        ["household_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_ledgers_household_shared",
        "ledgers",
        ["household_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'household_shared'"),
    )
    op.create_check_constraint(
        "ck_ledgers_ownership_scope",
        "ledgers",
        "(kind = 'household_shared' AND household_id IS NOT NULL AND owner_user_id IS NULL "
        "AND is_default = false) OR "
        "(kind <> 'household_shared' AND household_id IS NULL AND owner_user_id IS NOT NULL)",
    )


def downgrade() -> None:
    # Financial rows are never touched. Downgrade is intentionally refused if
    # household ledgers still contain data or references, because 0016 cannot
    # represent their authorization root without destructive rewriting.
    bind = op.get_bind()
    household_ledger_count = int(
        bind.execute(
            sa.text("SELECT count(*) FROM ledgers WHERE household_id IS NOT NULL")
        ).scalar_one()
    )
    if household_ledger_count:
        raise RuntimeError(
            "remove household spaces and their shared-ledger references before downgrading to 0016"
        )
    op.drop_constraint("ck_ledgers_ownership_scope", "ledgers", type_="check")
    op.drop_index("uq_ledgers_household_shared", table_name="ledgers")
    op.drop_constraint("fk_ledgers_household_id", "ledgers", type_="foreignkey")
    op.drop_column("ledgers", "household_id")
    op.alter_column("ledgers", "owner_user_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index("ix_household_invitations_expires", table_name="household_invitations")
    op.drop_index("ix_household_invitations_target_status", table_name="household_invitations")
    op.drop_index("uq_household_invitations_active_target", table_name="household_invitations")
    op.drop_table("household_invitations")
    op.drop_index("ix_household_members_user_status", table_name="household_members")
    op.drop_index("uq_household_members_active_owner", table_name="household_members")
    op.drop_table("household_members")
    op.drop_index("ix_households_owner_created", table_name="households")
    op.drop_table("households")
