"""Add ledger-scoped, period-specific monthly budgets (P28 Budget 2.0).

The new ``budgets`` table holds explicit monthly limits (the ledger total with
``category IS NULL`` and per-category limits otherwise). It layers on top of the
legacy recurring ``category_budgets`` table, which is left untouched so existing
budgets keep working; a period row wins for its month over a recurring budget.
The legacy table and its data are not modified, so the migration is lossless.

Revision ID: 20260809_0022
Revises: 20260809_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0022"
down_revision: str | None = "20260809_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IS NULL OR length(category) > 0", name="ck_budgets_category_nonempty"
        ),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_id", "period", "category", name="uq_budgets_ledger_period_category"
        ),
    )
    op.create_index(
        "uq_budgets_ledger_period_total",
        "budgets",
        ["ledger_id", "period"],
        unique=True,
        postgresql_where=sa.text("category IS NULL"),
    )
    op.create_index("ix_budgets_ledger_period", "budgets", ["ledger_id", "period"])


def downgrade() -> None:
    op.drop_index("ix_budgets_ledger_period", table_name="budgets")
    op.drop_index("uq_budgets_ledger_period_total", table_name="budgets")
    op.drop_table("budgets")
