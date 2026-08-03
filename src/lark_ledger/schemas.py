from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lark_ledger.models import Direction


class Action(StrEnum):
    CREATE = "create"
    CREATE_ENTRIES = "create_entries"
    UPDATE_LAST = "update_last"
    UNDO_LAST = "undo_last"
    SUMMARY = "summary"
    REPORT = "report"
    SET_BUDGET = "set_budget"
    SET_BUDGETS = "set_budgets"
    LIST_BUDGETS = "list_budgets"
    DELETE_BUDGET = "delete_budget"
    HELP = "help"


SUPPORTED_INPUT_CURRENCIES = frozenset(
    {"CNY", "USD", "EUR", "JPY", "GBP", "HKD", "KRW", "AUD", "CAD", "SGD"}
)


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
    budgets: list[BudgetCandidate] | None = Field(default=None, min_length=1, max_length=10)
    entries: list[EntryCandidate] | None = Field(default=None, min_length=1, max_length=20)
    batch_truncated: bool = False

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
        elif self.entries is not None or self.batch_truncated:
            raise ValueError("entries and batch_truncated are only supported for create_entries")
        if self.action in {Action.SUMMARY, Action.REPORT}:
            if self.range_start is None or self.range_end is None:
                raise ValueError(f"{self.action} requires range_start and range_end")
            if self.range_start >= self.range_end:
                raise ValueError(f"{self.action} range must be increasing")
        if self.action is Action.UPDATE_LAST and all(
            value is None
            for value in (self.amount, self.direction, self.category, self.note, self.occurred_at)
        ):
            raise ValueError("update_last requires at least one changed field")
        if self.currency is not None:
            if self.amount is None:
                raise ValueError("currency requires amount")
            if self.action not in {Action.CREATE, Action.UPDATE_LAST, Action.SET_BUDGET}:
                raise ValueError(f"currency is not supported for {self.action}")
        if self.action is Action.SET_BUDGET:
            missing = [name for name in ("amount", "category") if getattr(self, name) is None]
            if missing:
                raise ValueError(f"set_budget is missing: {', '.join(missing)}")
        if self.action is Action.SET_BUDGETS:
            if not self.budgets:
                raise ValueError("set_budgets requires at least one budget candidate")
            if any(value is not None for value in (self.amount, self.currency, self.category)):
                raise ValueError("set_budgets only accepts the budgets field")
        elif self.budgets is not None:
            raise ValueError("budgets is only supported for set_budgets")
        if self.action is Action.DELETE_BUDGET and self.category is None:
            raise ValueError("delete_budget requires category")
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


class ExecutionResult(BaseModel):
    message: str
    report: ReportData | None = None
    budget_alert: str | None = None
