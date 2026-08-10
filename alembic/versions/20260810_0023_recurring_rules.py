"""Add recurring rules and their occurrence ledger (P29).

New tables:

* ``recurring_rules`` — a known future recurring income / expense for one
  ledger, bound to a ledger-scoped account. The worker generates confirmation
  pendings (never direct transactions) when ``next_occurrence`` comes due.
  ``anchor_day`` keeps the day-of-month stable across month boundaries.
* ``recurring_occurrences`` — the deterministic identity of one scheduled
  period. ``(rule_id, occurrence_date)`` is unique, which is the database-level
  idempotency guarantee that retries / concurrent workers can never produce two
  pendings for the same period.

``pending_commands`` gains nullable ``recurring_rule_id`` + ``occurrence_date``
so a generated pending names its occurrence and confirming can mark it
``confirmed`` and link the created transaction. Existing pending rows keep NULL
and are unaffected.

Revision ID: 20260810_0023
Revises: 20260809_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260810_0023"
down_revision: str | None = "20260809_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recurring_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("creator_user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "transaction_type",
            postgresql.ENUM(
                "expense",
                "income",
                name="entry_direction",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("next_occurrence", sa.Date(), nullable=False),
        sa.Column("anchor_day", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
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
        sa.CheckConstraint("amount > 0", name="ck_recurring_rules_positive_amount"),
        sa.CheckConstraint(
            "frequency IN ('weekly', 'monthly', 'yearly')",
            name="ck_recurring_rules_frequency",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'disabled')", name="ck_recurring_rules_status"
        ),
        sa.CheckConstraint(
            "transaction_type IN ('EXPENSE', 'INCOME')",
            name="ck_recurring_rules_transaction_type",
        ),
        sa.CheckConstraint("interval >= 1", name="ck_recurring_rules_interval"),
        sa.CheckConstraint(
            "anchor_day BETWEEN 1 AND 31", name="ck_recurring_rules_anchor_day"
        ),
        sa.ForeignKeyConstraint(["creator_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_recurring_rules_ledger_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_id", "id", name="uq_recurring_rules_ledger_id"),
    )
    op.create_index("ix_recurring_rules_due", "recurring_rules", ["status", "next_occurrence"])
    op.create_index(
        "ix_recurring_rules_ledger_created",
        "recurring_rules",
        ["ledger_id", "created_at"],
    )

    op.create_table(
        "recurring_occurrences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("pending_id", sa.Uuid(), nullable=True),
        sa.Column("entry_id", sa.Uuid(), nullable=True),
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
            "status IN ('pending', 'skipped', 'confirmed', 'cancelled', 'failed')",
            name="ck_recurring_occurrences_status",
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["ledger_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pending_id"], ["pending_commands.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "rule_id"],
            ["recurring_rules.ledger_id", "recurring_rules.id"],
            name="fk_recurring_occurrences_ledger_rule",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_id", "occurrence_date", name="uq_recurring_occurrences_rule_date"
        ),
    )
    op.create_index(
        "ix_recurring_occurrences_rule_date",
        "recurring_occurrences",
        ["rule_id", "occurrence_date"],
    )

    op.add_column(
        "pending_commands",
        sa.Column("recurring_rule_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "pending_commands",
        sa.Column("occurrence_date", sa.Date(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pending_commands_recurring_rule",
        "pending_commands",
        "recurring_rules",
        ["recurring_rule_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_pending_commands_recurring_rule",
        "pending_commands",
        ["recurring_rule_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_commands_recurring_rule", table_name="pending_commands")
    op.drop_constraint(
        "fk_pending_commands_recurring_rule", "pending_commands", type_="foreignkey"
    )
    op.drop_column("pending_commands", "occurrence_date")
    op.drop_column("pending_commands", "recurring_rule_id")
    op.drop_index("ix_recurring_occurrences_rule_date", table_name="recurring_occurrences")
    op.drop_table("recurring_occurrences")
    op.drop_index("ix_recurring_rules_ledger_created", table_name="recurring_rules")
    op.drop_index("ix_recurring_rules_due", table_name="recurring_rules")
    op.drop_table("recurring_rules")
    # The ``entry_direction`` enum is shared with ledger_entries; leave it in place.
