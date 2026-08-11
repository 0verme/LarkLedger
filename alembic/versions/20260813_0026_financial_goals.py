"""Add financial goals and goal-account bindings (P33).

New tables:

* ``financial_goals`` — a savings target on top of real ledger facts. The goal
  stores only the user-defined plan (name, target amount, currency, optional
  target date, user-managed status). ``current_amount`` / ``progress_percent``
  are deliberately **not** columns: progress is recomputed deterministically
  from live account balances, so goals can never drift from the ledger and a
  goal can never hold a second parallel balance.
* ``goal_account_bindings`` — which real accounts count toward a goal.
  ``(ledger_id, goal_id)`` and ``(ledger_id, account_id)`` composite foreign
  keys keep every binding inside its own ledger; deleting a goal cascades its
  bindings without touching accounts, entries or transfers.

Downgrade drops both tables; existing ledger data needs no repair.

Revision ID: 20260813_0026
Revises: 20260812_0025
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260813_0026"
down_revision: str | None = "20260812_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "financial_goals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("goal_type", sa.String(16), nullable=False, server_default="savings"),
        sa.Column("target_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CNY"),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name="fk_financial_goals_ledger", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_financial_goals_creator",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_id", "id", name="uq_financial_goals_ledger_id"),
        sa.CheckConstraint("target_amount > 0", name="ck_financial_goals_positive_target"),
        sa.CheckConstraint("goal_type IN ('savings')", name="ck_financial_goals_type"),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'archived')", name="ck_financial_goals_status"
        ),
        sa.CheckConstraint(
            "length(name) > 0 AND length(name) <= 64", name="ck_financial_goals_name"
        ),
    )
    op.create_index(
        "ix_financial_goals_ledger_status",
        "financial_goals",
        ["ledger_id", "status", "created_at"],
    )
    op.create_table(
        "goal_account_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "goal_id"],
            ["financial_goals.ledger_id", "financial_goals.id"],
            name="fk_goal_bindings_ledger_goal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_goal_bindings_ledger_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("goal_id", "account_id", name="uq_goal_account_bindings_pair"),
        sa.UniqueConstraint("ledger_id", "id", name="uq_goal_account_bindings_ledger_id"),
    )
    op.create_index("ix_goal_bindings_goal", "goal_account_bindings", ["goal_id"])
    op.create_index("ix_goal_bindings_account", "goal_account_bindings", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_goal_bindings_account", table_name="goal_account_bindings")
    op.drop_index("ix_goal_bindings_goal", table_name="goal_account_bindings")
    op.drop_table("goal_account_bindings")
    op.drop_index("ix_financial_goals_ledger_status", table_name="financial_goals")
    op.drop_table("financial_goals")
