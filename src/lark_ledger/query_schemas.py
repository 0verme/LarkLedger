"""P46 — Deterministic ledger query contracts (transport-neutral).

Every future AI/assistant query must consume this layer and only this layer:

    QueryIntent（用户意图，已过 schema 校验）
        → QueryPlan（服务端授权 / 名称解析 / 范围归一化后的确定性执行计划）
            → LedgerQueryService
                → QueryResult（确定性事实，不携带 SQL）

Red lines (enforced by ``tests/architecture/test_assistant_query_guards.py``):

* This module is **database-free**: it never imports ``sqlalchemy`` or
  ``lark_ledger.db`` and never touches an ORM session. Contracts are pure
  Pydantic values an adapter or the future NL planner can construct safely.
* Every model uses ``extra="forbid"``: unknown fields are rejected. There is
  **no** ``sql`` / ``where`` / ``expression`` / ``raw_*`` / ``custom_filter``
  escape hatch — SQL-shaped payloads fail validation before they reach a query
  executor.
* ``Direction`` is reused from ``lark_ledger.models`` — no second enum is
  invented. Category/account filters are **names** (never ids owned by the
  client); the server resolves them to stable ids inside
  ``services.query_planner``.
* Monetary values are ``Decimal`` only. Time ranges are always
  timezone-aware and interpreted as left-closed / right-open ``[start, end)``.
* There is **no** ``merchant`` / ``category_group`` / ``custom_filter`` field:
  those concepts do not exist in the ledger data model and must never be
  silently mapped to ``note``/``category``. A query that mentions them is
  rejected at schema level (unknown field) rather than guessed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lark_ledger.models import Direction

# Web dashboard already caps page_size at 100 (le=100, default 25); the neutral
# query contract reuses that convention so adopters see one pagination rule.
DEFAULT_QUERY_PAGE_SIZE = 25
MAX_QUERY_PAGE_SIZE = 100
#: Deterministic Top N bound; a larger limit is a schema error, never a guess.
MAX_QUERY_TOP_N = 50
#: Matches the report / analytics ceiling: aggregate & group computation is
#: bounded to one civil year to keep results deterministic and verifiable.
MAX_QUERY_RANGE_DAYS = 366
MAX_QUERY_KEYWORD_LENGTH = 100
#: Source references are evidence hints, not an unbounded export channel.
MAX_QUERY_PROVENANCE_IDS = 100


class QueryMode(StrEnum):
    """What the query is trying to produce."""

    LIST = "list"
    AGGREGATE = "aggregate"
    GROUP = "group"


class QueryStatus(StrEnum):
    """Typed outcome of a query.

    Presenters must branch on this value, never on free-form message text:

    * ``ok`` — executed, matching rows / aggregates / groups exist;
    * ``empty`` — executed, zero matching rows (a normal controlled result);
    * ``invalid`` — intent passed schema but violates a planning rule (e.g.
      range exceeds the 366-day bound);
    * ``needs_clarification`` — a name could not be resolved deterministically
      (unknown / ambiguous account, or an invisible account);
    * ``unsupported`` — a recognized capability the executor does not
      implement (defense in depth; see ``QueryResult.unsupported``).
    """

    OK = "ok"
    EMPTY = "empty"
    INVALID = "invalid"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class QueryGrouping(StrEnum):
    CATEGORY = "category"
    ACCOUNT = "account"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class QuerySort(StrEnum):
    OCCURRED_AT = "occurred_at"
    AMOUNT = "amount"


class QueryOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class QueryMetric(StrEnum):
    """Deterministic measure used by an optional comparison analysis."""

    EXPENSE = "expense"
    INCOME = "income"
    CASH_OUTFLOW = "cash_outflow"
    CASH_INFLOW = "cash_inflow"


class QueryAnalysis(BaseModel):
    """Input-level analysis options carried by the assistant intent.

    The current period stays on ``QueryIntent``.  A comparison adds an
    explicit baseline period; omitting it requests a current-period cash-flow
    or spending analysis without inventing a baseline.
    """

    model_config = ConfigDict(extra="forbid")

    metric: QueryMetric = QueryMetric.EXPENSE
    baseline_start: datetime | None = None
    baseline_end: datetime | None = None

    @field_validator("baseline_start", "baseline_end")
    @classmethod
    def _baseline_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("baseline range must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _validate_baseline(self) -> QueryAnalysis:
        if (self.baseline_start is None) != (self.baseline_end is None):
            raise ValueError("baseline_start and baseline_end must be provided together")
        if (
            self.baseline_start is not None
            and self.baseline_end is not None
            and self.baseline_start >= self.baseline_end
        ):
            raise ValueError("baseline range must be increasing: [start, end)")
        return self


class QueryIntent(BaseModel):
    """Validated but still "user-intent level" query.

    This is the **only** type a natural-language planner / AI may produce for
    queries. It carries no SQL, no ORM objects, no execution details, and no
    database ids for account scope.
    """

    model_config = ConfigDict(extra="forbid")

    mode: QueryMode = QueryMode.LIST
    # Left-closed / right-open [start, end), always timezone-aware (naive
    # datetimes are rejected). Both-or-neither; start < end.
    start: datetime | None = None
    end: datetime | None = None
    # IANA name used to (a) interpret grouping buckets day/week/month and (b)
    # report the normalized period. Optional: defaults to the service
    # timezone; the resolved value is always echoed back in QueryResult.
    timezone: str | None = None
    direction: Direction | None = None
    # Exact server-side category string (categories are free-form strings in
    # the data model; there is no category registry to "resolve").
    category: str | None = Field(default=None, max_length=64)
    # Ledger-scoped account **name**, not an id. Resolved to a stable, visible
    # account inside QueryPlanner.  Unknown / ambiguous / invisible names are a
    # controlled needs_clarification, never an id invented by the client.
    account: str | None = Field(default=None, max_length=64)
    min_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    max_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    # note/category substring search. There is intentionally no "merchant"
    # field to silently map onto note.
    keyword: str | None = Field(default=None, max_length=MAX_QUERY_KEYWORD_LENGTH)
    sort: QuerySort = QuerySort.OCCURRED_AT
    order: QueryOrder = QueryOrder.DESC
    grouping: QueryGrouping | None = None
    # Requires ``grouping``; always explicit and capped (MAX_QUERY_TOP_N).
    top_n: int | None = Field(default=None, ge=1, le=MAX_QUERY_TOP_N)
    analysis: QueryAnalysis | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_QUERY_PAGE_SIZE, ge=1, le=MAX_QUERY_PAGE_SIZE)

    @field_validator("start", "end")
    @classmethod
    def _timezone_aware(cls, value: datetime | None) -> datetime | None:
        """Explicit timezone semantics: naive datetimes are rejected."""
        if value is not None and value.tzinfo is None:
            raise ValueError("time range must be timezone-aware (naive datetime is rejected)")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except Exception as exc:  # ZoneInfo raises Exception subclasses (KeyError/ValueError)
                raise ValueError(f"invalid IANA timezone: {value}") from exc
        return value

    @field_validator("category", "account")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("keyword")
    @classmethod
    def _normalize_keyword(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _validate_combinations(self) -> QueryIntent:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("time range must be increasing: [start, end)")
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            raise ValueError("min_amount must not exceed max_amount")
        if self.mode is QueryMode.GROUP:
            if self.grouping is None:
                raise ValueError("group mode requires a grouping")
        elif self.grouping is not None:
            raise ValueError("grouping is only supported in group mode")
        if self.top_n is not None:
            if self.grouping is None:
                raise ValueError("top_n requires a grouping")
        if self.analysis is not None and self.mode not in {
            QueryMode.AGGREGATE,
            QueryMode.GROUP,
        }:
            raise ValueError("analysis is only supported in aggregate or group mode")
        if self.mode is not QueryMode.LIST:
            # Aggregate and group are bounded computations (see
            # MAX_QUERY_RANGE_DAYS) so they always require an explicit range;
            # this mirrors the SUMMARY / REPORT convention in ParsedCommand.
            if self.start is None:
                raise ValueError(f"{self.mode.value} mode requires an explicit time range")
        return self


class QueryPlan(BaseModel):
    """Deterministic, server-side execution plan.

    Produced by ``QueryPlanner`` after authorization + name resolution + scope
    normalization. It contains **resolved business facts only** — never raw
    SQL, never a SQLAlchemy statement, never an ORM object. ``privacy_applied``
    is a fact about the plan; the privacy scope is applied as a real SQL
    condition inside ``LedgerQueryService`` (before aggregation).
    """

    model_config = ConfigDict(extra="forbid")

    mode: QueryMode
    ledger_id: uuid.UUID
    start: datetime | None  # normalized to UTC [start, end)
    end: datetime | None
    timezone: str  # resolved IANA name for bucketing / period reporting
    direction: Direction | None
    category: str | None  # exact matched category string
    account_id: uuid.UUID | None  # resolved, ledger-scoped, privacy-visible
    account_name: str | None
    min_amount: Decimal | None
    max_amount: Decimal | None
    keyword: str | None
    sort: QuerySort
    order: QueryOrder
    grouping: QueryGrouping | None
    top_n: int | None
    page: int
    page_size: int
    privacy_applied: bool
    analysis: QueryAnalysis | None


class QueryPagination(BaseModel):
    page: int
    page_size: int
    total: int
    pages: int


class QueryPeriod(BaseModel):
    start: datetime | None  # UTC
    end: datetime | None  # UTC
    timezone: str


class QueryFilters(BaseModel):
    """The applied filters as resolved facts, so a consumer can prove what the
    result actually covers (anti-leak / auditable)."""

    direction: Direction | None
    category: str | None
    account_id: uuid.UUID | None
    account_name: str | None
    min_amount: Decimal | None
    max_amount: Decimal | None
    keyword: str | None
    sort: QuerySort
    order: QueryOrder
    grouping: QueryGrouping | None
    top_n: int | None
    privacy_applied: bool


class QueryEntry(BaseModel):
    """One deterministic entry fact. Transfers never appear here."""

    short_id: str
    amount: Decimal
    currency: str
    direction: Direction
    category: str
    note: str
    occurred_at: datetime  # UTC
    account_id: uuid.UUID | None
    account_name: str | None


class QueryAggregate(BaseModel):
    currency: str
    income: Decimal
    expense: Decimal
    balance: Decimal
    count: int


class QueryGroup(BaseModel):
    key: str
    amount: Decimal
    count: int


class QueryAggregation(BaseModel):
    """Explain which deterministic operation produced a result."""

    model_config = ConfigDict(extra="forbid")

    mode: QueryMode
    grouping: QueryGrouping | None = None
    metric: str = Field(default="amount", min_length=1, max_length=32)


class QueryProvenance(BaseModel):
    """Bounded, privacy-safe evidence for a deterministic query result."""

    model_config = ConfigDict(extra="forbid")

    period: QueryPeriod
    filters: QueryFilters
    aggregation: QueryAggregation
    source_count: int = Field(ge=0)
    source_short_ids: list[str] = Field(
        default_factory=list, max_length=MAX_QUERY_PROVENANCE_IDS
    )
    source_ids_truncated: bool = False
    as_of: datetime


class QueryFactTotals(BaseModel):
    """Separate consumption, income and transfer facts for #16."""

    model_config = ConfigDict(extra="forbid")

    currency: str
    consumption: Decimal
    income: Decimal
    transfer_out: Decimal
    transfer_in: Decimal
    cash_outflow: Decimal
    cash_inflow: Decimal
    entry_count: int = Field(ge=0)
    transfer_count: int = Field(ge=0)


class QueryDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    amount: Decimal
    ratio: Decimal
    count: int = Field(ge=0)


class QueryComparisonGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    current: Decimal
    baseline: Decimal
    delta: Decimal
    current_count: int = Field(ge=0)
    baseline_count: int = Field(ge=0)


class QueryTrendPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: str
    consumption: Decimal
    income: Decimal
    transfer_out: Decimal
    transfer_in: Decimal


class QueryAnalysisResult(BaseModel):
    """Deterministic comparison / cash-flow facts for assistant presenters."""

    model_config = ConfigDict(extra="forbid")

    metric: QueryMetric
    current: QueryFactTotals
    baseline: QueryFactTotals | None = None
    delta: Decimal | None = None
    percentage: Decimal | None = None
    baseline_available: bool = True
    unavailable_reason: str | None = None
    top_contributors: list[QueryComparisonGroup] = Field(default_factory=list)
    distribution: list[QueryDistribution] = Field(default_factory=list)
    trend: list[QueryTrendPoint] = Field(default_factory=list)


class AssistantTextBlock(BaseModel):
    """Safe text fallback; never interpreted as markup by a presenter."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=2000)


class AssistantMetricBlock(BaseModel):
    """One deterministic monetary fact for cards and compact layouts."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["metric"]
    label: str = Field(min_length=1, max_length=64)
    value: Decimal
    currency: str = Field(min_length=3, max_length=3)
    delta: Decimal | None = None
    percentage: Decimal | None = None


class AssistantTableBlock(BaseModel):
    """Bounded, already-formatted table data from deterministic facts."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["table"]
    columns: list[str] = Field(min_length=1, max_length=12)
    rows: list[list[str]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_rows(self) -> AssistantTableBlock:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("table rows must match the column count")
        if any(len(cell) > 500 for row in self.rows for cell in row):
            raise ValueError("table cells are too long")
        return self


class AssistantChartPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    value: Decimal


class AssistantChartBlock(BaseModel):
    """Small chart dataset; the client chooses the visual treatment."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["chart"]
    chart: Literal["bar", "line"]
    label: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)
    points: list[AssistantChartPoint] = Field(min_length=1, max_length=100)


class AssistantEntriesBlock(BaseModel):
    """Bounded ledger rows for an evidence/drill-down renderer."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["entries"]
    entries: list[QueryEntry] = Field(max_length=100)


class AssistantLinkBlock(BaseModel):
    """Internal navigation only; external URLs and executable schemes are out."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["link"]
    label: str = Field(min_length=1, max_length=100)
    href: str = Field(min_length=1, max_length=500)

    @field_validator("href")
    @classmethod
    def internal_only(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("assistant links must be internal paths")
        return value


AssistantResponseBlock = Annotated[
    AssistantTextBlock
    | AssistantMetricBlock
    | AssistantTableBlock
    | AssistantChartBlock
    | AssistantEntriesBlock
    | AssistantLinkBlock,
    Field(discriminator="type"),
]


class QueryResult(BaseModel):
    """Deterministic query result — facts, not a free-form message.

    ``message`` is only a short, deterministic presentation hint; presenters
    must branch on ``status`` and the structured fields. ``provenance`` carries
    bounded, privacy-safe source references for drill-down; ``analysis`` carries
    deterministic comparison/cash-flow facts. ``unsupported`` / ``clarification``
    are the controlled, structured escape hatches adapters may surface without
    parsing exception strings.
    """

    model_config = ConfigDict(extra="forbid")

    status: QueryStatus
    mode: QueryMode
    message: str = Field(default="", max_length=2000)
    items: list[QueryEntry] = Field(default_factory=list)
    total_count: int = 0
    aggregates: QueryAggregate | None = None
    groups: list[QueryGroup] | None = None
    pagination: QueryPagination | None = None
    period: QueryPeriod | None = None
    filters: QueryFilters | None = None
    provenance: QueryProvenance | None = None
    analysis: QueryAnalysisResult | None = None
    #: Recognized-but-not-implemented capability names (defense in depth).
    unsupported: list[str] = Field(default_factory=list)
    #: Why a needs_clarification was returned (structured, not an exception).
    clarification: list[str] = Field(default_factory=list)
    invalid_reason: str | None = None
    #: Typed presentation facts. ``message`` remains the backwards-compatible
    #: fallback for Feishu and older clients.
    blocks: list[AssistantResponseBlock] = Field(default_factory=list, max_length=20)


def assistant_blocks_for_query(result: QueryResult) -> list[AssistantResponseBlock]:
    """Build bounded presentation blocks from deterministic query facts.

    This helper deliberately accepts only a ``QueryResult``. The model never
    supplies HTML, component names, URLs, or numeric values for these blocks.
    """

    blocks: list[AssistantResponseBlock] = []
    currency = result.aggregates.currency if result.aggregates is not None else "CNY"
    if result.aggregates is not None:
        blocks.extend(
            [
                AssistantMetricBlock(
                    type="metric", label="收入", value=result.aggregates.income, currency=currency
                ),
                AssistantMetricBlock(
                    type="metric", label="支出", value=result.aggregates.expense, currency=currency
                ),
                AssistantMetricBlock(
                    type="metric", label="结余", value=result.aggregates.balance, currency=currency
                ),
            ]
        )
    if result.groups:
        blocks.append(
            AssistantTableBlock(
                type="table",
                columns=["分组", "金额", "笔数"],
                rows=[[group.key, str(group.amount), str(group.count)] for group in result.groups],
            )
        )
    if result.items:
        blocks.append(AssistantEntriesBlock(type="entries", entries=result.items[:100]))
    if result.analysis is not None and result.analysis.trend:
        blocks.append(
            AssistantChartBlock(
                type="chart",
                chart="line",
                label="趋势",
                currency=currency,
                points=[
                    AssistantChartPoint(label=point.period, value=point.consumption)
                    for point in result.analysis.trend[:100]
                ],
            )
        )
    if not blocks and result.message:
        blocks.append(AssistantTextBlock(type="text", text=result.message))
    return blocks[:20]
