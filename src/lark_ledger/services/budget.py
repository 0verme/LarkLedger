"""Ledger-scoped, period-specific budget domain (P28 Budget 2.0).

The service owns both the write commands (set / delete a period budget) and the
read model (a unified "plan vs actual" progress overview). Budget limits live in
``Budget`` (period rows) layered on top of the legacy recurring
``CategoryBudget`` rows; actual spending is always recomputed from the live
``LedgerEntry`` facts in one ``GROUP BY`` query, so delete / restore / revision
never drift a cached counter and transfers (a separate table) can never be
counted as budget usage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Budget, CategoryBudget, Direction, LedgerEntry
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.web_schemas import BudgetItem, BudgetOverview

MAX_MONEY = Decimal("999999999999.99")
BUDGET_WARNING_THRESHOLD = 80
WARNING_PERCENT = Decimal(BUDGET_WARNING_THRESHOLD)
CURRENCY_LENGTH = 3


def parse_period(value: str) -> date:
    """Parse a ``YYYY-MM`` period key into the first day of that month."""
    raw = value.strip()
    parts = raw.split("-")
    if len(parts) != 2:
        raise ValueError("period must be in YYYY-MM format")
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError("period must be in YYYY-MM format") from exc
    if not 1 <= month <= 12:
        raise ValueError("period month must be between 1 and 12")
    return date(year, month, 1)


def period_key(period: date) -> str:
    """Render a period date as its ``YYYY-MM`` key."""
    return f"{period.year:04d}-{period.month:02d}"


def normalize_period(period: date) -> date:
    """Normalize any date to the first day of its month."""
    return period.replace(day=1)


class BudgetService:
    """Commands and the progress query for period-scoped budgets."""

    def __init__(self, session: AsyncSession, *, currency: str, timezone: str) -> None:
        self._session = session
        self._currency = currency
        self._timezone = ZoneInfo(timezone)

    # -- commands ---------------------------------------------------------

    async def set_total_budget(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        amount: Decimal,
        currency: str | None = None,
        now: datetime | None = None,
    ) -> BudgetOverview:
        await self._authorize(context)
        amount = self._validate_amount(amount)
        normalized = normalize_period(period or self._current_period(now))
        row = await self._period_budget(context.ledger_id, normalized, None)
        currency_code = self._resolve_currency(currency)
        if row is None:
            self._session.add(
                Budget(
                    ledger_id=context.ledger_id,
                    period=normalized,
                    category=None,
                    amount=amount,
                    currency=currency_code,
                )
            )
        else:
            row.amount = amount
            row.currency = currency_code
        await self._session.flush()
        return await self.overview(context, period=normalized)

    async def set_category_budget(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        category: str,
        amount: Decimal,
        currency: str | None = None,
        now: datetime | None = None,
    ) -> BudgetOverview:
        await self._authorize(context)
        name = category.strip()
        if not name:
            raise ValueError("category is required")
        if len(name) > 64:
            raise ValueError("category name must be at most 64 characters")
        amount = self._validate_amount(amount)
        normalized = normalize_period(period or self._current_period(now))
        row = await self._period_budget(context.ledger_id, normalized, name)
        currency_code = self._resolve_currency(currency)
        if row is None:
            self._session.add(
                Budget(
                    ledger_id=context.ledger_id,
                    period=normalized,
                    category=name,
                    amount=amount,
                    currency=currency_code,
                )
            )
        else:
            row.amount = amount
            row.currency = currency_code
        await self._session.flush()
        return await self.overview(context, period=normalized)

    async def delete_budget(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        category: str | None = None,
        now: datetime | None = None,
    ) -> BudgetOverview:
        await self._authorize(context)
        normalized = normalize_period(period or self._current_period(now))
        name = category.strip() if category else None
        row = await self._period_budget(context.ledger_id, normalized, name)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()
        return await self.overview(context, period=normalized)

    # -- progress query ---------------------------------------------------

    async def overview(
        self,
        scope: RequestContext | str,
        *,
        period: date | None = None,
        now: datetime | None = None,
    ) -> BudgetOverview:
        current = self._current_period(now)
        normalized = normalize_period(period or current)
        ledger_id = self._ledger_id(scope)

        period_start, period_end = self._period_bounds(normalized)
        spent_by_category = await self._monthly_spend(scope, period_start, period_end)
        total_spent = sum(spent_by_category.values(), Decimal("0"))

        explicit_total: Decimal | None = None
        period_rows: list[Budget] = []
        if ledger_id is not None:
            period_rows = list(
                (
                    await self._session.scalars(
                        select(Budget).where(
                            Budget.ledger_id == ledger_id,
                            Budget.period == normalized,
                        )
                    )
                ).all()
            )
            explicit_total = next(
                (row.amount for row in period_rows if row.category is None), None
            )
        period_by_category: dict[str, Decimal] = {
            str(row.category): row.amount for row in period_rows if row.category is not None
        }

        recurring_rows = list(
            (
                await self._session.scalars(
                    select(CategoryBudget).where(self._recurring_scope(scope))
                )
            ).all()
        )
        recurring_by_category = {row.category: row.amount for row in recurring_rows}

        categories = sorted(
            set(spent_by_category) | set(period_by_category) | set(recurring_by_category)
        )
        items: list[BudgetItem] = []
        for category in categories:
            budget_amount = period_by_category.get(category, recurring_by_category.get(category))
            spent = spent_by_category.get(category, Decimal("0"))
            items.append(
                BudgetItem(
                    category=category,
                    amount=budget_amount,
                    spent=spent,
                    remaining=self._remaining(budget_amount, spent),
                    usage_rate=self._usage_rate(budget_amount, spent),
                    status=self._status(budget_amount, spent),
                )
            )
        allocated = sum((item.amount for item in items if item.amount is not None), Decimal("0"))
        if explicit_total is not None:
            total_budget = explicit_total
        elif allocated:
            total_budget = allocated
        else:
            total_budget = None
        remaining, usage_rate, status = self._overall_progress(total_budget, total_spent)
        return BudgetOverview(
            currency=self._currency,
            period=period_key(normalized),
            total_budget=total_budget,
            total_spent=total_spent,
            total_remaining=remaining,
            usage_rate=usage_rate,
            status=status,
            total_limit_set=explicit_total is not None,
            allocated=allocated,
            unallocated=explicit_total - allocated if explicit_total is not None else None,
            items=items,
        )

    # -- helpers ----------------------------------------------------------

    async def _authorize(self, context: RequestContext) -> None:
        await LedgerAuthorizationService(self._session).get_accessible(
            context.actor_user_id, context.ledger_id
        )

    async def _period_budget(
        self, ledger_id: uuid.UUID, period: date, category: str | None
    ) -> Budget | None:
        return (
            await self._session.execute(
                select(Budget).where(
                    Budget.ledger_id == ledger_id,
                    Budget.period == period,
                    Budget.category == category,
                )
            )
        ).scalar_one_or_none()

    async def _monthly_spend(
        self, scope: RequestContext | str, start: datetime, end: datetime
    ) -> dict[str, Decimal]:
        """Sum expense amounts per category for ``[start, end)`` in one query."""
        rows = (
            await self._session.execute(
                select(LedgerEntry.category, func.sum(LedgerEntry.amount).label("total")).where(
                    self._entry_scope(scope),
                    LedgerEntry.direction == Direction.EXPENSE,
                    LedgerEntry.deleted_at.is_(None),
                    LedgerEntry.occurred_at >= start,
                    LedgerEntry.occurred_at < end,
                ).group_by(LedgerEntry.category)
            )
        ).all()
        return {str(category): Decimal(total) for category, total in rows}

    def _period_bounds(self, period: date) -> tuple[datetime, datetime]:
        start = datetime.combine(period, time.min, tzinfo=self._timezone).astimezone(UTC)
        if period.month == 12:
            next_start = datetime.combine(
                date(period.year + 1, 1, 1), time.min, tzinfo=self._timezone
            )
        else:
            next_start = datetime.combine(
                date(period.year, period.month + 1, 1), time.min, tzinfo=self._timezone
            )
        return start, next_start.astimezone(UTC)

    @staticmethod
    def _remaining(budget: Decimal | None, spent: Decimal) -> Decimal | None:
        return None if budget is None else budget - spent

    @staticmethod
    def _usage_rate(budget: Decimal | None, spent: Decimal) -> Decimal | None:
        if budget is None:
            return None
        return (spent / budget * 100).quantize(Decimal("0.01"))

    @staticmethod
    def _status(budget: Decimal | None, spent: Decimal) -> str:
        if budget is None:
            return "none"
        if spent > budget:
            return "exceeded"
        if spent * 100 >= budget * WARNING_PERCENT:
            return "warning"
        return "normal"

    @staticmethod
    def _overall_progress(
        budget: Decimal | None, spent: Decimal
    ) -> tuple[Decimal | None, Decimal | None, str]:
        remaining = None if budget is None else budget - spent
        usage = None if budget is None else (spent / budget * 100).quantize(Decimal("0.01"))
        status = "none" if budget is None else BudgetService._status(budget, spent)
        return remaining, usage, status

    @staticmethod
    def _validate_amount(amount: Decimal) -> Decimal:
        if amount <= 0:
            raise ValueError("budget amount must be at least 0.01")
        if amount > MAX_MONEY:
            raise ValueError("budget amount exceeds the supported limit")
        return amount

    def _resolve_currency(self, currency: str | None) -> str:
        code = (currency or self._currency).strip().upper()
        if len(code) != CURRENCY_LENGTH or not code.isalpha():
            raise ValueError("currency must be a three-letter code")
        return code

    @staticmethod
    def _ledger_id(scope: RequestContext | str) -> uuid.UUID | None:
        if isinstance(scope, str):
            return None
        return scope.ledger_id

    def _current_period(self, now: datetime | None = None) -> date:
        return self._local(now or datetime.now(UTC)).date().replace(day=1)

    def _local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self._timezone)

    @staticmethod
    def _entry_scope(scope: RequestContext | str) -> Any:
        if isinstance(scope, str):
            return LedgerEntry.user_open_id == scope
        if scope.external_subject_id is None:
            return LedgerEntry.ledger_id == scope.ledger_id
        return or_(
            LedgerEntry.ledger_id == scope.ledger_id,
            and_(
                LedgerEntry.ledger_id.is_(None),
                LedgerEntry.user_open_id == scope.external_subject_id,
            ),
        )

    @staticmethod
    def _recurring_scope(scope: RequestContext | str) -> Any:
        if isinstance(scope, str):
            return CategoryBudget.user_open_id == scope
        if scope.external_subject_id is None:
            return CategoryBudget.ledger_id == scope.ledger_id
        return or_(
            CategoryBudget.ledger_id == scope.ledger_id,
            and_(
                CategoryBudget.ledger_id.is_(None),
                CategoryBudget.user_open_id == scope.external_subject_id,
            ),
        )
