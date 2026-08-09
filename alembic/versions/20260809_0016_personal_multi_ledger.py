"""Add persistent personal multi-ledger selection and contract constraints.

Revision ID: 20260809_0016
Revises: 20260809_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0016"
down_revision: str | None = "20260809_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalized_name(name: str) -> str:
    import re
    import unicodedata

    display = " ".join(unicodedata.normalize("NFKC", name).strip().split())
    return re.sub(r"[\s\-_·•・.。]+", "", display).casefold()


def upgrade() -> None:
    op.add_column("ledgers", sa.Column("normalized_name", sa.String(length=128), nullable=True))
    bind = op.get_bind()
    for ledger_id, name in bind.execute(sa.text("SELECT id, name FROM ledgers")):
        bind.execute(
            sa.text("UPDATE ledgers SET normalized_name = :normalized WHERE id = :id"),
            {"id": ledger_id, "normalized": _normalized_name(str(name))},
        )
    op.alter_column("ledgers", "normalized_name", existing_type=sa.String(128), nullable=False)
    op.create_unique_constraint(
        "uq_ledgers_owner_name", "ledgers", ["owner_user_id", "normalized_name"]
    )

    op.add_column("channel_identities", sa.Column("current_ledger_id", sa.Uuid(), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE channel_identities AS identity SET current_ledger_id = ledger.id "
            "FROM ledgers AS ledger WHERE ledger.owner_user_id = identity.user_id "
            "AND ledger.is_default = true"
        )
    )
    op.create_foreign_key(
        "fk_channel_identities_current_ledger_id",
        "channel_identities",
        "ledgers",
        ["current_ledger_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Re-run a lossless contract backfill for rows written during a rolling
    # 0015 deployment. Stage-1 application writes already populate these
    # fields, but this closes the old-code/new-schema overlap window before
    # users can create a second ledger.
    for table in ("ledger_entries", "category_budgets", "ledger_entry_revisions"):
        bind.execute(
            sa.text(
                f"UPDATE {table} AS item SET ledger_id = ledger.id "
                "FROM channel_identities AS identity "
                "JOIN ledgers AS ledger ON ledger.owner_user_id = identity.user_id "
                "AND ledger.is_default = true "
                "WHERE item.ledger_id IS NULL AND identity.channel = 'feishu' "
                "AND identity.external_subject_id = item.user_open_id"
            )
        )
    bind.execute(
        sa.text(
            "UPDATE ledger_entry_revisions AS item SET actor_user_id = identity.user_id "
            "FROM channel_identities AS identity WHERE item.actor_user_id IS NULL "
            "AND identity.channel = 'feishu' "
            "AND identity.external_subject_id = item.user_open_id"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE pending_commands AS item SET actor_user_id = identity.user_id, "
            "ledger_id = ledger.id FROM channel_identities AS identity "
            "JOIN ledgers AS ledger ON ledger.owner_user_id = identity.user_id "
            "AND ledger.is_default = true WHERE identity.channel = 'feishu' "
            "AND identity.external_subject_id = item.user_open_id "
            "AND (item.actor_user_id IS NULL OR item.ledger_id IS NULL)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE dashboard_sessions AS item SET user_id = identity.user_id, "
            "ledger_id = ledger.id FROM channel_identities AS identity "
            "JOIN ledgers AS ledger ON ledger.owner_user_id = identity.user_id "
            "AND ledger.is_default = true WHERE identity.channel = 'feishu' "
            "AND identity.external_subject_id = item.user_open_id "
            "AND (item.user_id IS NULL OR item.ledger_id IS NULL)"
        )
    )
    unresolved = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM ledger_entries WHERE ledger_id IS NULL) + "
            "(SELECT count(*) FROM category_budgets WHERE ledger_id IS NULL) + "
            "(SELECT count(*) FROM ledger_entry_revisions WHERE ledger_id IS NULL) + "
            "(SELECT count(*) FROM pending_commands "
            " WHERE ledger_id IS NULL OR actor_user_id IS NULL) + "
            "(SELECT count(*) FROM dashboard_sessions "
            " WHERE ledger_id IS NULL OR user_id IS NULL)"
        )
    ).scalar_one()
    if int(unresolved) != 0:
        raise RuntimeError(
            "identity ledger backfill is incomplete; refusing multi-ledger migration"
        )

    op.drop_constraint("uq_entries_user_short_id", "ledger_entries", type_="unique")
    op.drop_constraint("uq_budgets_user_category", "category_budgets", type_="unique")
    op.drop_index("uq_pending_user_active_fingerprint", table_name="pending_commands")
    op.create_index(
        "uq_pending_ledger_active_fingerprint",
        "pending_commands",
        ["ledger_id", "source_fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "source_fingerprint IS NOT NULL AND status IN ('pending', 'executing')"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("uq_pending_ledger_active_fingerprint", table_name="pending_commands")
    # Multi-ledger rows may legitimately collide on these legacy keys. Restore
    # the old constraints only when doing so is lossless; never rewrite or drop
    # user financial data merely to make a downgrade succeed.
    fingerprint_conflicts = bind.execute(
        sa.text(
            "SELECT 1 FROM pending_commands WHERE source_fingerprint IS NOT NULL "
            "AND status IN ('pending', 'executing') "
            "GROUP BY user_open_id, source_fingerprint HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if fingerprint_conflicts is None:
        op.create_index(
            "uq_pending_user_active_fingerprint",
            "pending_commands",
            ["user_open_id", "source_fingerprint"],
            unique=True,
            postgresql_where=sa.text(
                "source_fingerprint IS NOT NULL AND status IN ('pending', 'executing')"
            ),
        )
    entry_conflicts = bind.execute(
        sa.text(
            "SELECT 1 FROM ledger_entries GROUP BY user_open_id, short_id "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if entry_conflicts is None:
        op.create_unique_constraint(
            "uq_entries_user_short_id", "ledger_entries", ["user_open_id", "short_id"]
        )
    budget_conflicts = bind.execute(
        sa.text(
            "SELECT 1 FROM category_budgets GROUP BY user_open_id, category "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if budget_conflicts is None:
        op.create_unique_constraint(
            "uq_budgets_user_category", "category_budgets", ["user_open_id", "category"]
        )
    op.drop_constraint(
        "fk_channel_identities_current_ledger_id", "channel_identities", type_="foreignkey"
    )
    op.drop_column("channel_identities", "current_ledger_id")
    op.drop_constraint("uq_ledgers_owner_name", "ledgers", type_="unique")
    op.drop_column("ledgers", "normalized_name")
