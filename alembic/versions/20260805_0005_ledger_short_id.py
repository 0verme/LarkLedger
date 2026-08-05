"""Add user-scoped five-character ledger short IDs.

Revision ID: 20260805_0005
Revises: 20260805_0004

Upgrade:
- Adds ledger_entries.short_id
- Backfills existing rows with Crockford Base32 codes unique per user
- Enforces UNIQUE(user_open_id, short_id) and NOT NULL

Downgrade data loss:
- Drops short_id and the unique constraint. Chat references become invalid.
- UUID primary keys and ledger amounts are preserved.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260805_0005"
down_revision: str | None = "20260805_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep alphabet local to the migration so upgrade does not import application modules.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LENGTH = 5
_MAX_ATTEMPTS = 64


def _random_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def upgrade() -> None:
    op.add_column("ledger_entries", sa.Column("short_id", sa.String(length=5), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, user_open_id FROM ledger_entries ORDER BY user_open_id, id")
    ).fetchall()

    used_by_user: dict[str, set[str]] = {}
    for row_id, user_open_id in rows:
        used = used_by_user.setdefault(str(user_open_id), set())
        code: str | None = None
        for _ in range(_MAX_ATTEMPTS):
            candidate = _random_code()
            if candidate not in used:
                code = candidate
                break
        if code is None:
            raise RuntimeError(
                f"failed to allocate short_id during migration for user {user_open_id}"
            )
        used.add(code)
        connection.execute(
            sa.text("UPDATE ledger_entries SET short_id = :short_id WHERE id = :id"),
            {"short_id": code, "id": row_id},
        )

    # Safety checks before tightening constraints.
    nulls = connection.execute(
        sa.text("SELECT COUNT(*) FROM ledger_entries WHERE short_id IS NULL")
    ).scalar()
    if nulls:
        raise RuntimeError(f"short_id backfill left {nulls} NULL rows")

    dupes = connection.execute(
        sa.text(
            "SELECT user_open_id, short_id, COUNT(*) AS c "
            "FROM ledger_entries "
            "GROUP BY user_open_id, short_id "
            "HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        raise RuntimeError(f"short_id backfill produced duplicates: {dupes[:5]!r}")

    op.create_unique_constraint(
        "uq_entries_user_short_id",
        "ledger_entries",
        ["user_open_id", "short_id"],
    )
    op.alter_column("ledger_entries", "short_id", existing_type=sa.String(length=5), nullable=False)


def downgrade() -> None:
    op.drop_constraint("uq_entries_user_short_id", "ledger_entries", type_="unique")
    op.drop_column("ledger_entries", "short_id")
