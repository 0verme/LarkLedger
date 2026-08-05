"""Stable ledger entry revision snapshots (no ORM __dict__ dumps)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from lark_ledger.models import Direction, LedgerEntry

SNAPSHOT_VERSION: Final[int] = 1


class RevisionChangeType(StrEnum):
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"


def snapshot_ledger_entry(entry: LedgerEntry) -> dict[str, Any]:
    """Serialize audit fields with JSON-stable primitive types."""
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "entry_id": str(entry.id),
        "short_id": entry.short_id,
        "amount": _decimal_str(entry.amount),
        "currency": entry.currency,
        "direction": (
            entry.direction.value
            if isinstance(entry.direction, Direction)
            else str(entry.direction)
        ),
        "category": entry.category,
        "note": entry.note,
        "occurred_at": _dt_iso(entry.occurred_at),
        "source_type": entry.source_type,
        "deleted_at": _dt_iso(entry.deleted_at) if entry.deleted_at is not None else None,
        "updated_at": _dt_iso(entry.updated_at) if entry.updated_at is not None else None,
    }


def _decimal_str(value: Decimal) -> str:
    return format(value, "f")


def _dt_iso(value: datetime) -> str:
    return value.isoformat()
