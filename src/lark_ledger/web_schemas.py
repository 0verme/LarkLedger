from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lark_ledger.client_schemas import ClientTransfer
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
    # Ledger-scoped account binding (P26). account_name is denormalized from the
    # account row at read time and never leaks other ledgers' accounts.
    account_id: str
    account_name: str | None = None


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


class TransferList(BaseModel):
    items: list[ClientTransfer]
    page: int
    page_size: int
    total: int
    pages: int


class WebTransferDetail(BaseModel):
    transfer: ClientTransfer
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


class WebLedger(BaseModel):
    id: str
    name: str
    is_default: bool
    is_current: bool
    currency: str
    timezone: str
    kind: str
    household_id: str | None = None


class LedgerList(BaseModel):
    items: list[WebLedger]


class LedgerNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class HouseholdCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class HouseholdInviteRequest(BaseModel):
    target: str = Field(min_length=1, max_length=128)


class WebHouseholdMember(BaseModel):
    user_id: str
    display_name: str
    role: str
    joined_at: datetime | None


class WebHousehold(BaseModel):
    id: str
    name: str
    owner_user_id: str
    role: str
    status: str
    ledger: WebLedger
    created_at: datetime
    updated_at: datetime
    members: list[WebHouseholdMember] | None = None


class HouseholdList(BaseModel):
    items: list[WebHousehold]


class WebHouseholdInvitation(BaseModel):
    id: str
    invitation_code: str
    household_id: str
    household_name: str
    target_user_id: str
    status: str
    expires_at: datetime
    created_at: datetime


class EntryUpdateRequest(BaseModel):
    """PATCH body for one ledger entry.

    ``extra="forbid"`` guarantees a typo'd or future unknown field (e.g. an
    ``account_id`` on a model that predates account support) is rejected instead
    of silently ignored, so a client can never believe an account change landed
    when it did not.
    """

    model_config = ConfigDict(extra="forbid")

    expected_updated_at: datetime
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    direction: Direction | None = None
    category: str | None = Field(default=None, min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None
    account_id: uuid.UUID | None = None

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
                self.account_id,
            )
        ):
            raise ValueError("at least one field must be updated")
        return self


class EntryCreateRequest(BaseModel):
    """POST body to create one ledger entry from the Web dashboard."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    direction: Direction
    category: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)
    occurred_at: datetime
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    account_id: uuid.UUID | None = None


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


class AdminEvent(BaseModel):
    event_id: str
    source_message_id: str | None
    status: str
    attempt_count: int
    transport: str | None
    received_at: datetime | None
    processed_at: datetime
    last_error_code: str | None
    updated_at: datetime


class AdminEventPage(BaseModel):
    items: list[AdminEvent]
    page: int
    page_size: int
    total: int
    pages: int


class AdminOutbox(BaseModel):
    id: str
    event_id: str | None
    reply_type: str
    sequence: int
    status: str
    attempt_count: int
    created_at: datetime
    sent_at: datetime | None
    last_error_code: str | None


class AdminOutboxPage(BaseModel):
    items: list[AdminOutbox]
    page: int
    page_size: int
    total: int
    pages: int


class AdminDeadSummary(BaseModel):
    event_count: int
    reply_count: int
    latest_events: list[AdminEvent]
    latest_replies: list[AdminOutbox]


class EventReplayRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=512)
    execute: bool = False
    confirmation_event_id: str | None = Field(default=None, max_length=128)


class ResultReplayResponse(BaseModel):
    reset: int
    skipped: int
    not_found: int


class SafeSystemConfig(BaseModel):
    version: str
    event_mode: str
    timezone: str
    currency: str
    worker_enabled: bool
    reply_worker_enabled: bool
    cleanup_worker_enabled: bool
    pending_enabled: bool
    ai_provider: str
    ai_model: str
    ai_api_key_configured: bool
    lark_app_secret_configured: bool
    dashboard_base_url: str
    session_ttl_seconds: int
    secure_cookie: bool


class AnalyticsSummary(BaseModel):
    range_start: datetime
    range_end: datetime
    income: Decimal
    expense: Decimal
    balance: Decimal
    entry_count: int


class AnalyticsTrendPoint(BaseModel):
    period: date
    income: Decimal
    expense: Decimal
    balance: Decimal


class AnalyticsCategory(BaseModel):
    category: str
    amount: Decimal
    ratio: Decimal


class AnalyticsMonthlyPoint(BaseModel):
    period: str
    income: Decimal
    expense: Decimal
    balance: Decimal


class AnalyticsOverview(BaseModel):
    summary: AnalyticsSummary
    trend: list[AnalyticsTrendPoint]
    categories: list[AnalyticsCategory]


class BudgetItem(BaseModel):
    category: str
    amount: Decimal
    spent: Decimal
    remaining: Decimal
    usage_rate: Decimal


class BudgetOverview(BaseModel):
    currency: str
    total_budget: Decimal
    total_spent: Decimal
    total_remaining: Decimal
    usage_rate: Decimal
    items: list[BudgetItem]


class BudgetUpdateRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class ExportRequestBody(BaseModel):
    preset: Literal["last_90_days", "this_month", "all", "custom"]
    start_date: date | None = None
    end_date: date | None = None
    include_deleted: bool = False

    @model_validator(mode="after")
    def validate_custom_range(self) -> ExportRequestBody:
        if self.preset == "custom":
            if self.start_date is None or self.end_date is None:
                raise ValueError("custom export requires start_date and end_date")
            if self.start_date > self.end_date:
                raise ValueError("start_date must not be after end_date")
        return self


DeletedFilter = Literal["active", "deleted", "all"]
EntrySort = Literal["occurred_at", "amount", "updated_at"]
SortOrder = Literal["asc", "desc"]
PendingGroup = Literal["pending", "completed", "closed"]
AdminEventStatus = Literal[
    "received", "processing", "failed", "succeeded", "dead", "legacy", "legacy_succeeded"
]
AdminOutboxStatus = Literal["pending", "sending", "failed", "sent", "dead"]
AnalyticsPeriod = Literal["7d", "30d", "90d", "year", "custom"]
