from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lark_ledger.models import Direction


class Action(StrEnum):
    CREATE = "create"
    UPDATE_LAST = "update_last"
    UNDO_LAST = "undo_last"
    SUMMARY = "summary"
    REPORT = "report"
    HELP = "help"


class ParsedCommand(BaseModel):
    """Only data the AI may return; there is intentionally no SQL-shaped field."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    direction: Direction | None = None
    category: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None

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
