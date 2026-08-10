import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lark_ledger.models import Direction


class Action(StrEnum):
    CREATE = "create"
    TRANSFER = "transfer"
    CREATE_ENTRIES = "create_entries"
    BATCH = "batch"
    UPDATE_LAST = "update_last"
    UNDO_LAST = "undo_last"
    LIST_ENTRIES = "list_entries"
    GET_ENTRY = "get_entry"
    UPDATE_ENTRY = "update_entry"
    DELETE_ENTRY = "delete_entry"
    RESTORE_ENTRY = "restore_entry"
    EXPORT_ENTRIES = "export_entries"
    SUMMARY = "summary"
    REPORT = "report"
    SET_BUDGET = "set_budget"
    SET_BUDGETS = "set_budgets"
    SET_TOTAL_BUDGET = "set_total_budget"
    LIST_BUDGETS = "list_budgets"
    DELETE_BUDGET = "delete_budget"
    LIST_ACCOUNTS = "list_accounts"
    ASSETS = "assets"
    HELP = "help"


SUPPORTED_INPUT_CURRENCIES = frozenset(
    {"CNY", "USD", "EUR", "JPY", "GBP", "HKD", "KRW", "AUD", "CAD", "SGD"}
)
MAX_BATCH_ENTRIES = 30
MAX_BATCH_BUDGETS = 10
DEFAULT_LIST_LIMIT = 10
MAX_LIST_LIMIT = 20
# CSV export limits (P04); keep magic numbers out of call sites.
MAX_EXPORT_ROWS = 5000
MAX_EXPORT_BYTES = 5 * 1024 * 1024
DEFAULT_EXPORT_DAYS = 90


class BudgetCandidate(BaseModel):
    """Potential batch item; each item is validated strictly before persistence."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    amount: Decimal | str | None = None
    currency: str | None = None


class EntryCandidate(BaseModel):
    """Loosely parsed image item that is validated before persistence."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal | str | None = None
    currency: str | None = None
    direction: Direction | str | None = None
    category: str | None = None
    note: str | None = None
    occurred_at: datetime | str | None = None


class ParsedCommand(BaseModel):
    """Only data the AI may return; there is intentionally no SQL-shaped field."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    direction: Direction | None = None
    category: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None
    budgets: list[BudgetCandidate] | None = Field(
        default=None, min_length=1, max_length=MAX_BATCH_BUDGETS
    )
    entries: list[EntryCandidate] | None = Field(
        default=None, min_length=1, max_length=MAX_BATCH_ENTRIES
    )
    batch_truncated: bool = False
    budgets_truncated: bool = False
    # Chat short-ID reference (optional leading #); used by get/update/delete/restore.
    entry_ref: str | None = Field(default=None, max_length=16)
    before_entry_ref: str | None = Field(default=None, max_length=16)
    # Upper bound is enforced in LedgerService (cap + user notice), not Schema max.
    limit: int | None = Field(default=None, ge=1, le=100)
    # Distinguish "note not provided" (False) from explicit clear (True).
    clear_note: bool = False
    # export_entries only: full history when user explicitly asks for 全部/所有/完整历史.
    export_all: bool = False
    # export_entries only: include soft-deleted rows when user explicitly asks.
    include_deleted: bool = False
    # AI/user supplied names only. Stable IDs are resolved by deterministic domain code.
    from_account_hint: str | None = Field(default=None, min_length=1, max_length=64)
    to_account_hint: str | None = Field(default=None, min_length=1, max_length=64)
    # Ledger-scoped account name for create / update_last / update_entry and the
    # optional single-account filter of list_accounts. Resolved server-side to an
    # Account id; the AI never returns or invents account_id.
    account_hint: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.upper()
        if normalized not in SUPPORTED_INPUT_CURRENCIES:
            raise ValueError(f"unsupported input currency: {normalized}")
        return normalized

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ParsedCommand":
        if self.action is Action.CREATE:
            missing = [
                name
                for name in ("amount", "direction", "category", "occurred_at")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"create is missing: {', '.join(missing)}")
        if self.action is Action.TRANSFER:
            missing = [
                name
                for name in ("amount", "occurred_at", "from_account_hint", "to_account_hint")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"transfer is missing: {', '.join(missing)}")
            if self.direction is not None or self.category is not None:
                raise ValueError("transfer does not accept direction or category")
        elif self.from_account_hint is not None or self.to_account_hint is not None:
            raise ValueError("from/to account hints are only supported for transfer")
        if self.account_hint is not None and self.action not in {
            Action.CREATE,
            Action.UPDATE_LAST,
            Action.UPDATE_ENTRY,
            Action.LIST_ACCOUNTS,
        }:
            raise ValueError(f"account_hint is not supported for {self.action}")
        if self.action is Action.CREATE_ENTRIES:
            if not self.entries:
                raise ValueError("create_entries requires at least one entry candidate")
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.currency,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.range_start,
                    self.range_end,
                    self.budgets,
                )
            ):
                raise ValueError("create_entries only accepts entries and batch_truncated")
            if self.budgets_truncated:
                raise ValueError("budgets_truncated is only supported for batch")
        elif self.action is Action.BATCH:
            if not self.entries and not self.budgets:
                raise ValueError("batch requires entries or budgets")
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.currency,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.range_start,
                    self.range_end,
                )
            ):
                raise ValueError("batch only accepts entries, budgets, and truncation flags")
            if self.batch_truncated and not self.entries:
                raise ValueError("batch_truncated requires entries")
            if self.budgets_truncated and not self.budgets:
                raise ValueError("budgets_truncated requires budgets")
        elif (
            self.entries is not None
            or self.batch_truncated
            or (self.budgets_truncated and self.action is not Action.SET_BUDGETS)
        ):
            raise ValueError("batch fields are only supported for create_entries or batch")
        if self.action in {Action.SUMMARY, Action.REPORT}:
            if self.range_start is None or self.range_end is None:
                raise ValueError(f"{self.action} requires range_start and range_end")
            if self.range_start >= self.range_end:
                raise ValueError(f"{self.action} range must be increasing")
        if self.action is Action.LIST_ENTRIES:
            if self.range_start is not None and self.range_end is not None:
                if self.range_start >= self.range_end:
                    raise ValueError("list_entries range must be increasing")
            elif self.range_start is not None or self.range_end is not None:
                raise ValueError("list_entries range requires both range_start and range_end")
            if self.amount is not None or self.currency is not None or self.note is not None:
                raise ValueError("list_entries does not accept amount, currency, or note")
            if self.occurred_at is not None or self.entry_ref is not None:
                raise ValueError("list_entries does not accept occurred_at or entry_ref")
            if self.entries is not None or self.budgets is not None:
                raise ValueError("list_entries does not accept entries or budgets")
        if self.action is Action.GET_ENTRY:
            if self.entry_ref is None or not str(self.entry_ref).strip():
                raise ValueError("get_entry requires entry_ref")
            if self.clear_note:
                raise ValueError("get_entry does not accept clear_note")
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.currency,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.range_start,
                    self.range_end,
                    self.limit,
                    self.before_entry_ref,
                    self.entries,
                    self.budgets,
                )
            ):
                raise ValueError("get_entry only accepts entry_ref")
        if self.action is Action.UPDATE_ENTRY:
            if self.entry_ref is None or not str(self.entry_ref).strip():
                raise ValueError("update_entry requires entry_ref")
            # No "at least one changed field" requirement here: the web / client
            # APIs pass account_id out-of-band (never via this AI-facing schema),
            # so an account-only update legitimately carries no changed command
            # fields. A genuinely empty command is handled as a no-op by the
            # service layer.
            if self.range_start is not None or self.range_end is not None:
                raise ValueError("update_entry does not accept range filters")
            if self.limit is not None or self.before_entry_ref is not None:
                raise ValueError("update_entry does not accept list pagination fields")
            if self.entries is not None or self.budgets is not None:
                raise ValueError("update_entry does not accept batch fields")
            if self.clear_note and self.note is not None:
                raise ValueError("clear_note cannot be combined with note")
        if self.action in {Action.DELETE_ENTRY, Action.RESTORE_ENTRY}:
            if self.entry_ref is None or not str(self.entry_ref).strip():
                raise ValueError(f"{self.action} requires entry_ref")
            if self.clear_note:
                raise ValueError(f"{self.action} does not accept clear_note")
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.currency,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.range_start,
                    self.range_end,
                    self.limit,
                    self.before_entry_ref,
                    self.entries,
                    self.budgets,
                )
            ):
                raise ValueError(f"{self.action} only accepts entry_ref")
        if self.action is Action.EXPORT_ENTRIES:
            if self.clear_note:
                raise ValueError("export_entries does not accept clear_note")
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.currency,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.limit,
                    self.entry_ref,
                    self.before_entry_ref,
                    self.entries,
                    self.budgets,
                )
            ):
                raise ValueError(
                    "export_entries only accepts range_start, range_end, "
                    "export_all, and include_deleted"
                )
            if self.export_all:
                # Explicit full history; optional ranges are ignored at the service layer.
                pass
            elif self.range_start is not None and self.range_end is not None:
                if self.range_start >= self.range_end:
                    raise ValueError("export_entries range must be increasing")
            elif self.range_start is not None or self.range_end is not None:
                raise ValueError("export_entries range requires both range_start and range_end")
        if self.action in {Action.LIST_ACCOUNTS, Action.ASSETS}:
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.currency,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.range_start,
                    self.range_end,
                    self.limit,
                    self.entry_ref,
                    self.before_entry_ref,
                    self.entries,
                    self.budgets,
                    self.from_account_hint,
                    self.to_account_hint,
                )
            ):
                raise ValueError(f"{self.action} only accepts account_hint")
            if self.action is Action.ASSETS and self.account_hint is not None:
                raise ValueError("assets does not accept account_hint")
        if (
            self.action is Action.UPDATE_LAST
            and all(
                value is None
                for value in (
                    self.amount,
                    self.direction,
                    self.category,
                    self.note,
                    self.occurred_at,
                    self.account_hint,
                )
            )
            and not self.clear_note
        ):
            raise ValueError("update_last requires at least one changed field")
        if self.action is Action.UPDATE_LAST and self.clear_note and self.note is not None:
            raise ValueError("clear_note cannot be combined with note")
        if self.currency is not None:
            if self.amount is None:
                raise ValueError("currency requires amount")
            if self.action not in {
                Action.CREATE,
                Action.TRANSFER,
                Action.UPDATE_LAST,
                Action.UPDATE_ENTRY,
                Action.SET_BUDGET,
                Action.SET_TOTAL_BUDGET,
            }:
                raise ValueError(f"currency is not supported for {self.action}")
        if self.action is Action.SET_BUDGET:
            missing = [name for name in ("amount", "category") if getattr(self, name) is None]
            if missing:
                raise ValueError(f"set_budget is missing: {', '.join(missing)}")
        if self.action is Action.SET_TOTAL_BUDGET:
            if self.amount is None:
                raise ValueError("set_total_budget requires amount")
            if self.category is not None:
                raise ValueError("set_total_budget does not accept category")
        if self.action is Action.SET_BUDGETS:
            if not self.budgets:
                raise ValueError("set_budgets requires at least one budget candidate")
            if any(value is not None for value in (self.amount, self.currency, self.category)):
                raise ValueError("set_budgets only accepts the budgets field")
        elif self.action is not Action.BATCH and self.budgets is not None:
            raise ValueError("budgets is only supported for set_budgets")
        if self.action is Action.DELETE_BUDGET and self.category is None:
            raise ValueError("delete_budget requires category")
        entry_ref_actions = {
            Action.LIST_ENTRIES,
            Action.GET_ENTRY,
            Action.UPDATE_ENTRY,
            Action.DELETE_ENTRY,
            Action.RESTORE_ENTRY,
        }
        if self.action not in entry_ref_actions:
            if self.entry_ref is not None or self.before_entry_ref is not None:
                raise ValueError("entry_ref fields are only supported for entry lookup/mutation")
            if self.limit is not None:
                raise ValueError("limit is only supported for list_entries")
        elif self.action is not Action.LIST_ENTRIES and self.limit is not None:
            raise ValueError("limit is only supported for list_entries")
        elif self.action is not Action.LIST_ENTRIES and self.before_entry_ref is not None:
            raise ValueError("before_entry_ref is only supported for list_entries")
        if self.clear_note and self.action not in {Action.UPDATE_LAST, Action.UPDATE_ENTRY}:
            raise ValueError("clear_note is only supported for update actions")
        if self.export_all and self.action is not Action.EXPORT_ENTRIES:
            raise ValueError("export_all is only supported for export_entries")
        if self.include_deleted and self.action is not Action.EXPORT_ENTRIES:
            raise ValueError("include_deleted is only supported for export_entries")
        return self


class SummaryItem(BaseModel):
    category: str
    amount: Decimal


class CategoryTotal(BaseModel):
    category: str
    amount: Decimal


class TrendPoint(BaseModel):
    period: date
    amount: Decimal


class ReportData(BaseModel):
    range_start: datetime
    range_end: datetime
    currency: str
    income_total: Decimal
    expense_total: Decimal
    balance: Decimal
    entry_count: int = Field(ge=0)
    categories: list[CategoryTotal]
    trend: list[TrendPoint]
    trend_granularity: Literal["day", "month"]


class AdviceResult(BaseModel):
    items: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=2, max_length=3
    )


class ExportFileResult(BaseModel):
    """In-memory CSV payload for Feishu file upload (not persisted)."""

    filename: str
    content: bytes
    row_count: int = Field(ge=0)
    range_label: str


class ExecutionResult(BaseModel):
    message: str
    report: ReportData | None = None
    budget_alert: str | None = None
    export: ExportFileResult | None = None
    # The ledger entry a write action created / mutated, when known. Set by the
    # entry-creation path so callers (e.g. the P29 recurring confirmation hook)
    # can link the transaction without re-querying.
    entry_id: uuid.UUID | None = None
