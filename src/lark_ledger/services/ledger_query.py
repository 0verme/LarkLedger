"""P46 — Deterministic, ledger-scoped, privacy-safe query execution.

``LedgerQueryService`` is the single neutral query executor for the assistant
layer. It consumes a ``QueryPlan`` (already authorized + resolved + normalized
by ``QueryPlanner``) and returns a deterministic ``QueryResult``.

Guarantees:

* **privacy before aggregation** — the account-visibility condition is applied
  as real SQL in the ``WHERE`` clause before any sum / count / group / Top N,
  so a private account can never leak through an indirect channel;
* **ledger-scoped** — every query is bound to ``RequestContext.ledger_id``;
* **transfers excluded** — the service queries ``LedgerEntry`` only; a
  ``Transfer`` is never counted as income, expense, category consumption or a
  group member;
* **deterministic** — stable sort with explicit tie-breakers, Decimal-only
  accounting, ``[start, end)`` ranges, and a controlled ``unsupported`` /
  ``empty`` / ``invalid`` / ``needs_clarification`` status instead of guessing.

The SQL-building logic here mirrors ``WebLedgerQueryService`` because this
layer is deliberately the transport-neutral facts layer (no WebEntry /
dashboard presentation); the shared building blocks — ``PrivacyService``,
``LedgerAuthorizationService`` and the stable-ordering convention — are reused,
never re-implemented.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Account, Direction, LedgerEntry
from lark_ledger.query_schemas import (
    QueryAggregate,
    QueryEntry,
    QueryFilters,
    QueryGroup,
    QueryGrouping,
    QueryIntent,
    QueryMode,
    QueryOrder,
    QueryPagination,
    QueryPeriod,
    QueryPlan,
    QueryResult,
    QuerySort,
    QueryStatus,
)
from lark_ledger.services.privacy import PrivacyService
from lark_ledger.services.query_planner import QueryPlanner

#: Executor capability allowlists. A future ``QueryMode`` / ``QueryGrouping``
#: enum value that is not implemented here must surface as a controlled
#: ``unsupported`` instead of being guessed.
_SUPPORTED_MODES = frozenset({QueryMode.LIST, QueryMode.AGGREGATE, QueryMode.GROUP})
_SUPPORTED_GROUPINGS = frozenset(
    {
        QueryGrouping.CATEGORY,
        QueryGrouping.ACCOUNT,
        QueryGrouping.DAY,
        QueryGrouping.WEEK,
        QueryGrouping.MONTH,
    }
)

_EMPTY_MESSAGE = "没有符合条件的账目。"


class LedgerQueryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        timezone: str = "Asia/Shanghai",
        currency: str = "CNY",
    ) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency

    async def query(self, context: RequestContext, intent: QueryIntent) -> QueryResult:
        """Full flow: plan (authorize + resolve + normalize) then execute."""
        plan = await QueryPlanner(
            self._session, timezone=str(self._timezone), currency=self._currency
        ).plan(context, intent)
        if isinstance(plan, QueryResult):
            return plan
        return await self.execute(context, plan)

    async def execute(self, context: RequestContext, plan: QueryPlan) -> QueryResult:
        if plan.mode not in _SUPPORTED_MODES:
            return self._unsupported(plan, [f"mode:{plan.mode}"])
        if plan.grouping is not None and plan.grouping not in _SUPPORTED_GROUPINGS:
            return self._unsupported(plan, [f"grouping:{plan.grouping}"])
        filters = await self._build_filters(context, plan)
        if plan.mode is QueryMode.LIST:
            return await self._execute_list(context, plan, filters)
        if plan.mode is QueryMode.AGGREGATE:
            return await self._execute_aggregate(context, plan, filters)
        return await self._execute_group(context, plan, filters)

    # ------------------------------------------------------------------ #
    # Shared filter construction (privacy BEFORE aggregation)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _entry_scope(context: RequestContext) -> Any:
        """Ledger-scoped with the nullable legacy fallback, matching the
        established web/ledger convention."""
        if context.external_subject_id is None:
            return LedgerEntry.ledger_id == context.ledger_id
        return or_(
            LedgerEntry.ledger_id == context.ledger_id,
            and_(
                LedgerEntry.ledger_id.is_(None),
                LedgerEntry.user_open_id == context.external_subject_id,
            ),
        )

    async def _build_filters(
        self, context: RequestContext, plan: QueryPlan
    ) -> list[Any]:
        filters: list[Any] = [
            self._entry_scope(context),
            LedgerEntry.deleted_at.is_(None),
        ]
        privacy = await PrivacyService(self._session).entry_visibility_scope(context)
        if privacy is not None:
            filters.append(privacy)
        if plan.start is not None:
            filters.append(LedgerEntry.occurred_at >= plan.start)
        if plan.end is not None:
            filters.append(LedgerEntry.occurred_at < plan.end)
        if plan.direction is not None:
            filters.append(LedgerEntry.direction == plan.direction)
        if plan.category is not None:
            filters.append(LedgerEntry.category == plan.category)
        if plan.account_id is not None:
            filters.append(LedgerEntry.account_id == plan.account_id)
        if plan.min_amount is not None:
            filters.append(LedgerEntry.amount >= plan.min_amount)
        if plan.max_amount is not None:
            filters.append(LedgerEntry.amount <= plan.max_amount)
        if plan.keyword:
            filters.append(
                or_(
                    LedgerEntry.note.icontains(plan.keyword, autoescape=True),
                    LedgerEntry.category.icontains(plan.keyword, autoescape=True),
                )
            )
        return filters

    # ------------------------------------------------------------------ #
    # Entry list (stable pagination)
    # ------------------------------------------------------------------ #

    async def _execute_list(
        self, context: RequestContext, plan: QueryPlan, filters: list[Any]
    ) -> QueryResult:
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(LedgerEntry).where(*filters)
            )
            or 0
        )
        sort_column = {
            QuerySort.OCCURRED_AT: LedgerEntry.occurred_at,
            QuerySort.AMOUNT: LedgerEntry.amount,
        }[plan.sort]
        rows = (
            (
                await self._session.scalars(
                    select(LedgerEntry)
                    .where(*filters)
                    .order_by(*self._order_by(sort_column, plan.order))
                    .offset((plan.page - 1) * plan.page_size)
                    .limit(plan.page_size)
                )
            )
            .all()
        )
        names = await self._account_names({row.account_id for row in rows})
        items = [
            QueryEntry(
                short_id=row.short_id,
                amount=row.amount,
                currency=row.currency,
                direction=row.direction,
                category=row.category,
                note=row.note,
                occurred_at=row.occurred_at,
                account_id=row.account_id,
                account_name=names.get(row.account_id),
            )
            for row in rows
        ]
        pages = ceil(total / plan.page_size) if total else 0
        status = QueryStatus.OK if total else QueryStatus.EMPTY
        message = f"第 {plan.page}/{pages} 页，共 {total} 笔" if total else _EMPTY_MESSAGE
        return QueryResult(
            status=status,
            mode=QueryMode.LIST,
            message=message,
            items=items,
            total_count=total,
            pagination=QueryPagination(
                page=plan.page, page_size=plan.page_size, total=total, pages=pages
            ),
            period=self._period(plan),
            filters=self._filters(plan),
        )

    @staticmethod
    def _order_by(column: Any, order: QueryOrder) -> list[Any]:
        """Stable ordering: primary column + (created_at, id) tie-breakers,
        always in the requested direction (deterministic, never DB-natural)."""
        direction = "asc" if order is QueryOrder.ASC else "desc"
        return [
            getattr(column, direction)(),
            getattr(LedgerEntry.created_at, direction)(),
            getattr(LedgerEntry.id, direction)(),
        ]

    # ------------------------------------------------------------------ #
    # Aggregate total
    # ------------------------------------------------------------------ #

    async def _execute_aggregate(
        self, context: RequestContext, plan: QueryPlan, filters: list[Any]
    ) -> QueryResult:
        rows = (
            await self._session.execute(
                select(LedgerEntry.amount, LedgerEntry.direction).where(*filters)
            )
        ).all()
        count = len(rows)
        if count == 0:
            return QueryResult(
                status=QueryStatus.EMPTY,
                mode=QueryMode.AGGREGATE,
                message=_EMPTY_MESSAGE,
                total_count=0,
                period=self._period(plan),
                filters=self._filters(plan),
            )
        income = Decimal("0")
        expense = Decimal("0")
        for amount_value, direction in rows:
            amount = Decimal(amount_value)
            if direction is Direction.INCOME:
                income += amount
            else:
                expense += amount
        return QueryResult(
            status=QueryStatus.OK,
            mode=QueryMode.AGGREGATE,
            message=self._aggregate_message(plan, income, expense, count),
            total_count=count,
            aggregates=QueryAggregate(
                currency=self._currency,
                income=income,
                expense=expense,
                balance=income - expense,
                count=count,
            ),
            period=self._period(plan),
            filters=self._filters(plan),
        )

    # ------------------------------------------------------------------ #
    # Grouping + Top N
    # ------------------------------------------------------------------ #

    async def _execute_group(
        self, context: RequestContext, plan: QueryPlan, filters: list[Any]
    ) -> QueryResult:
        rows = (
            await self._session.execute(
                select(
                    LedgerEntry.amount,
                    LedgerEntry.direction,
                    LedgerEntry.category,
                    LedgerEntry.account_id,
                    LedgerEntry.occurred_at,
                ).where(*filters)
            )
        ).all()
        if not rows:
            return QueryResult(
                status=QueryStatus.EMPTY,
                mode=QueryMode.GROUP,
                message=_EMPTY_MESSAGE,
                total_count=0,
                period=self._period(plan),
                filters=self._filters(plan),
            )
        names = await self._account_names(
            {row[3] for row in rows if row[3] is not None}
        )
        grouping = plan.grouping or QueryGrouping.CATEGORY
        zone = ZoneInfo(plan.timezone)
        total_by_key: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        count_by_key: defaultdict[str, int] = defaultdict(int)
        for amount_value, _direction, category, account_id, occurred_at in rows:
            amount = Decimal(amount_value)
            key = self._group_key(grouping, category, account_id, occurred_at, names, zone)
            total_by_key[key] += amount
            count_by_key[key] += 1
        ranked = sorted(
            total_by_key.items(), key=lambda pair: (-pair[1], pair[0])
        )
        if plan.top_n is not None:
            ranked = ranked[: plan.top_n]
        groups = [
            QueryGroup(key=key, amount=amount, count=count_by_key[key])
            for key, amount in ranked
        ]
        return QueryResult(
            status=QueryStatus.OK,
            mode=QueryMode.GROUP,
            message=f"{len(groups)} 个分组 · 共 {sum(group.count for group in groups)} 笔",
            total_count=len(rows),
            groups=groups,
            period=self._period(plan),
            filters=self._filters(plan),
        )

    def _group_key(
        self,
        grouping: QueryGrouping,
        category: str,
        account_id: uuid.UUID | None,
        occurred_at: datetime,
        names: dict[uuid.UUID | None, str],
        zone: ZoneInfo,
    ) -> str:
        if grouping is QueryGrouping.CATEGORY:
            return str(category)
        if grouping is QueryGrouping.ACCOUNT:
            # Legacy rows without an account group under ""; account keys are the
            # display name at query time (deterministic within one execution).
            return names.get(account_id) or ""
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        local_date = occurred_at.astimezone(zone).date()
        if grouping is QueryGrouping.DAY:
            return local_date.isoformat()
        if grouping is QueryGrouping.WEEK:
            # Weeks start on Monday; key = the Monday of the week.
            monday = local_date - timedelta(days=local_date.weekday())
            return monday.isoformat()
        return local_date.strftime("%Y-%m")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _period(self, plan: QueryPlan) -> QueryPeriod:
        return QueryPeriod(start=plan.start, end=plan.end, timezone=plan.timezone)

    async def _account_names(
        self, account_ids: set[Any]
    ) -> dict[uuid.UUID | None, str]:
        """Resolve display names for the requested ids only (visible rows only).
        ``None`` maps to the empty display name for legacy unbound rows."""
        names: dict[uuid.UUID | None, str] = {None: ""}
        ids = {account_id for account_id in account_ids if account_id is not None}
        if not ids:
            return names
        rows = (
            await self._session.execute(
                select(Account.id, Account.name).where(Account.id.in_(ids))
            )
        ).all()
        for account_id, name in rows:
            names[account_id] = name
        return names

    @staticmethod
    def _filters(plan: QueryPlan) -> QueryFilters:
        return QueryFilters(
            direction=plan.direction,
            category=plan.category,
            account_id=plan.account_id,
            account_name=plan.account_name,
            min_amount=plan.min_amount,
            max_amount=plan.max_amount,
            keyword=plan.keyword,
            sort=plan.sort,
            order=plan.order,
            grouping=plan.grouping,
            top_n=plan.top_n,
            privacy_applied=plan.privacy_applied,
        )

    def _money(self, value: Decimal) -> str:
        if self._currency == "CNY":
            return f"¥{value:.2f}"
        return f"{value:.2f} {self._currency}"

    def _aggregate_message(
        self, plan: QueryPlan, income: Decimal, expense: Decimal, count: int
    ) -> str:
        if plan.direction is Direction.INCOME:
            kind = f"收入 {self._money(income)}"
        elif plan.direction is Direction.EXPENSE:
            kind = f"支出 {self._money(expense)}"
        else:
            kind = (
                f"收入 {self._money(income)} · 支出 {self._money(expense)} · "
                f"结余 {self._money(income - expense)}"
            )
        return f"共 {count} 笔 · {kind}"

    @staticmethod
    def _unsupported(plan: QueryPlan, capabilities: list[str]) -> QueryResult:
        # The reply itself must always be a valid contract value: the offending
        # capability travels in ``unsupported`` (e.g. "mode:magic"), while the
        # echoed mode is clamped to a representable value.
        safe_mode = plan.mode if plan.mode in _SUPPORTED_MODES else QueryMode.LIST
        return QueryResult(
            status=QueryStatus.UNSUPPORTED,
            mode=safe_mode,
            message="当前查询能力暂不支持该语义。",
            unsupported=capabilities,
        )
