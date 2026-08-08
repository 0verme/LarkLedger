from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from lark_ledger.models import Direction


class WebEntry(BaseModel):
    id: str
    short_id: str
    amount: Decimal
    currency: str
    direction: Direction
    category: str
    note: str
    occurred_at: datetime
    source_type: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class EntryPage(BaseModel):
    items: list[WebEntry]
    page: int
    page_size: int
    total: int
    pages: int


class WebRevision(BaseModel):
    id: str
    change_type: str
    before: dict[str, Any]
    after: dict[str, Any]
    created_at: datetime


class EntryDetail(BaseModel):
    entry: WebEntry
    revisions: list[WebRevision]


class TrendValue(BaseModel):
    period: date
    income: Decimal
    expense: Decimal
    balance: Decimal


class CategoryValue(BaseModel):
    category: str
    amount: Decimal
    ratio: Decimal


class DashboardData(BaseModel):
    month_income: Decimal
    month_expense: Decimal
    month_balance: Decimal
    budget_usage_rate: Decimal | None
    pending_count: int
    recent_entries: list[WebEntry]
    trend: list[TrendValue]
    categories: list[CategoryValue]


class EntryUpdateRequest(BaseModel):
    expected_updated_at: datetime
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    direction: Direction | None = None
    category: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def has_change(self) -> EntryUpdateRequest:
        if all(
            value is None
            for value in (
                self.amount,
                self.direction,
                self.category,
                self.note,
                self.occurred_at,
            )
        ):
            raise ValueError("at least one field must be updated")
        return self


class EntryVersionRequest(BaseModel):
    expected_updated_at: datetime


class WebPending(BaseModel):
    confirmation_id: str
    status: str
    source_type: str
    transport: str
    risk_reason: str
    entries_total: int
    income_total: Decimal
    expense_total: Decimal
    currency: str
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None


class PendingPage(BaseModel):
    items: list[WebPending]
    page: int
    page_size: int
    total: int
    pages: int


class PendingDetail(BaseModel):
    pending: WebPending
    preview: dict[str, Any]


class PendingActionResponse(BaseModel):
    message: str
    pending: PendingDetail


DeletedFilter = Literal["active", "deleted", "all"]
EntrySort = Literal["occurred_at", "amount", "updated_at"]
SortOrder = Literal["asc", "desc"]
PendingGroup = Literal["pending", "completed", "closed"]
