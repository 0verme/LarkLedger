"""Add account-level privacy: visibility + owner (P32).

New columns:

* ``accounts.visibility`` — ``shared`` (default) or ``private``. Shared
  accounts are visible to every active member of the household ledger;
  private accounts are visible only to ``owner_user_id``. Personal-ledger
  behavior is unchanged: privacy filters are a no-op unless the ledger kind
  is ``household_shared``, and existing rows are backfilled to ``shared``
  with ``owner_user_id`` NULL.
* ``accounts.owner_user_id`` — the internal user who owns a private account
  (must be set whenever ``visibility = 'private'``, enforced by CHECK).

Downgrade refuses when any private account exists, because dropping the
columns would silently leak private data into shared reads.

Revision ID: 20260812_0025
Revises: 20260811_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260812_0025"
down_revision: str | None = "20260811_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "visibility",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'shared'"),
        ),
    )
    op.add_column(
        "accounts",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_accounts_owner_user_id",
        "accounts",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_accounts_visibility",
        "accounts",
        "visibility IN ('shared', 'private')",
    )
    op.create_check_constraint(
        "ck_accounts_private_owner",
        "accounts",
        "(visibility <> 'private') OR (owner_user_id IS NOT NULL)",
    )


def downgrade() -> None:
    conn = op.get_bind()
    private_count = conn.execute(
        sa.text("SELECT count(*) FROM accounts WHERE visibility = 'private'")
    ).scalar_one()
    if private_count:
        raise RuntimeError(
            "cannot downgrade below account privacy: "
            f"{private_count} private account(s) would lose their visibility"
        )
    op.drop_constraint("ck_accounts_private_owner", "accounts", type_="check")
    op.drop_constraint("ck_accounts_visibility", "accounts", type_="check")
    op.drop_constraint("fk_accounts_owner_user_id", "accounts", type_="foreignkey")
    op.drop_column("accounts", "owner_user_id")
    op.drop_column("accounts", "visibility")
