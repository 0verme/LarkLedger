"""CSV export of ledger entries (Schema v1).

Export is read-only: no DB writes, no revision rows, no long-lived files.
Formula-injection sanitization applies only to the export copy.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import MAX_EXPORT_BYTES, ExportFileResult
from lark_ledger.short_id import format_entry_ref

CSV_SCHEMA_VERSION: Final[str] = "v1"

# Fixed CSV Schema v1 column order. Do not add user_open_id / UUID / message ids.
CSV_HEADERS: Final[tuple[str, ...]] = (
    "short_id",
    "occurred_at",
    "direction",
    "amount",
    "currency",
    "category",
    "note",
    "source_type",
    "created_at",
    "updated_at",
    "deleted_at",
)

# First non-whitespace characters that spreadsheets may treat as formulas.
_FORMULA_TRIGGER_CHARS: Final[frozenset[str]] = frozenset({"=", "+", "-", "@"})
# Leading control characters that can also trigger formula interpretation.
_LEADING_CONTROL_CHARS: Final[frozenset[str]] = frozenset({"\t", "\r", "\n"})


class ExportTooLargeError(ValueError):
    """Raised when generated CSV exceeds MAX_EXPORT_BYTES."""


@dataclass(frozen=True)
class ExportRequest:
    """Normalized export parameters after business-layer defaults."""

    range_start: datetime | None
    range_end: datetime | None
    include_deleted: bool
    export_all: bool
    range_label: str


def sanitize_csv_cell(value: str) -> str:
    """Prefix a single quote when the cell could be interpreted as a formula.

    Does not mutate stored ledger data; only the export serialization path.
    """
    if not value:
        return value
    if value[0] in _LEADING_CONTROL_CHARS:
        return f"'{value}"
    first_non_ws = next((ch for ch in value if ch not in " \t\r\n"), None)
    if first_non_ws is not None and first_non_ws in _FORMULA_TRIGGER_CHARS:
        return f"'{value}"
    return value


def format_csv_amount(amount: Decimal) -> str:
    """Stable decimal string without scientific notation."""
    quantized = amount.quantize(Decimal("0.01"))
    return format(quantized, "f")


def format_csv_datetime(value: datetime | None, timezone: ZoneInfo) -> str:
    """ISO 8601 with timezone offset; empty string for None."""
    if value is None:
        return ""
    if value.tzinfo is None:
        local = value.replace(tzinfo=timezone)
    else:
        local = value.astimezone(timezone)
    return local.isoformat()


def format_csv_direction(direction: Direction) -> str:
    """Stable Direction enum value (expense / income)."""
    return direction.value


def entry_to_csv_row(entry: LedgerEntry, timezone: ZoneInfo) -> list[str]:
    """Map a ledger row to Schema v1 cells (sanitized where user-controlled)."""
    return [
        format_entry_ref(entry.short_id),
        format_csv_datetime(entry.occurred_at, timezone),
        format_csv_direction(entry.direction),
        format_csv_amount(entry.amount),
        entry.currency,
        sanitize_csv_cell(entry.category),
        sanitize_csv_cell(entry.note),
        entry.source_type,
        format_csv_datetime(entry.created_at, timezone),
        format_csv_datetime(entry.updated_at, timezone),
        format_csv_datetime(entry.deleted_at, timezone),
    ]


def build_export_filename(when: datetime) -> str:
    """Application-generated filename; never uses user input or open_id."""
    stamp = when.strftime("%Y%m%d-%H%M%S")
    return f"larkledger-export-{CSV_SCHEMA_VERSION}-{stamp}.csv"


def entries_to_csv_bytes(
    entries: Sequence[LedgerEntry],
    *,
    timezone: ZoneInfo,
    max_bytes: int = MAX_EXPORT_BYTES,
) -> bytes:
    """Serialize entries to UTF-8 BOM CSV bytes; raise if over max_bytes."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)
    for entry in entries:
        writer.writerow(entry_to_csv_row(entry, timezone))
    # utf-8-sig writes BOM so Windows Excel opens Chinese without mojibake.
    payload = buffer.getvalue().encode("utf-8-sig")
    if len(payload) > max_bytes:
        raise ExportTooLargeError(
            f"export CSV exceeds {max_bytes} bytes ({len(payload)} bytes)"
        )
    return payload


def build_export_file(
    entries: Sequence[LedgerEntry],
    *,
    timezone: ZoneInfo,
    when: datetime,
    range_label: str,
    max_bytes: int = MAX_EXPORT_BYTES,
) -> ExportFileResult:
    content = entries_to_csv_bytes(entries, timezone=timezone, max_bytes=max_bytes)
    return ExportFileResult(
        filename=build_export_filename(when),
        content=content,
        row_count=len(entries),
        range_label=range_label,
    )
