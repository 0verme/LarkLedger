"""Single source of truth for adapter-contract business facts (P36).

Every contract case (C01–C08) compares the Domain Result produced through the
Feishu adapter, the Web/Application adapter and the Client API against one
shared expectation defined here — never three sets of hand-written expected
values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import LedgerEntry, LedgerEntryRevision


@dataclass(frozen=True)
class CanonicalExpectation:
    """The one expected business fact an entry must satisfy on every channel."""

    direction: str
    amount: str
    currency: str
    category: str
    note: str
    created_by_user_id: str | None = None
    paid_by_user_id: str | None = None


async def entry_snapshot(
    session: AsyncSession, source_message_id: str
) -> dict[str, Any]:
    """Read a row's business facts (never transport metadata)."""
    row = await session.scalar(
        select(LedgerEntry).where(LedgerEntry.source_message_id == source_message_id)
    )
    assert row is not None, f"no entry for source {source_message_id}"
    return {
        "ledger_id": str(row.ledger_id),
        "direction": row.direction.value if hasattr(row.direction, "value") else str(row.direction),
        "amount": str(row.amount),
        "currency": row.currency,
        "category": row.category,
        "note": row.note,
        "created_by_user_id": str(row.created_by_user_id),
        "paid_by_user_id": str(row.paid_by_user_id) if row.paid_by_user_id else None,
        "deleted_at": row.deleted_at,
    }


def assert_matches(snapshot: dict[str, Any], expected: CanonicalExpectation) -> None:
    """Assert one row satisfies the canonical business expectation."""
    for field in (
        "direction",
        "amount",
        "currency",
        "category",
        "note",
        "created_by_user_id",
        "paid_by_user_id",
    ):
        expected_value = getattr(expected, field)
        if expected_value is None:
            continue
        assert snapshot[field] == expected_value, (
            f"field {field}: expected {expected_value!r}, got {snapshot[field]!r}"
        )


async def revision_count(session: AsyncSession, entry_id: Any) -> int:
    rows = (
        await session.scalars(
            select(LedgerEntryRevision).where(LedgerEntryRevision.entry_id == entry_id)
        )
    ).all()
    return len(rows)
