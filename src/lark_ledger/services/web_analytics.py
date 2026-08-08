"""User-scoped analytics and budget read models for the Dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import CategoryBudget, Direction, LedgerEntry
from lark_ledger.web_schemas import (
    AnalyticsCategory,
    AnalyticsMonthlyPoint,
    AnalyticsSummary,
    AnalyticsTrendPoint,
    BudgetItem,
    BudgetOverview,
)

MAX_ANALYTICS_DAYS = 366


def local_date_bounds(
    start_date: date, end_date: date, timezone: ZoneInfo
) -> tuple[datetime, datetime]:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if (end_date - start_date).days + 1 > MAX_ANALYTICS_DAYS:
        raise ValueError(f"analytics range must not exceed {MAX_ANALYTICS_DAYS} days")
    start = datetime.combine(start_date, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone).astimezone(
        UTC
    )
    return start, end


class WebAnalyticsQueryService:
    def __init__(self, session: AsyncSession, *, timezone: str, currency: str) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency

    async def analytics(
        self, user_open_id: RequestContext | str, *, start_date: date, end_date: date
    ) -> tuple[
        AnalyticsSummary,
        list[AnalyticsTrendPoint],
        list[AnalyticsCategory],
        list[AnalyticsMonthlyPoint],
    ]:
        start, end = local_date_bounds(start_date, end_date, self._timezone)
        rows = (
            await self._session.execute(
                select(
                    LedgerEntry.amount,
                    LedgerEntry.direction,
                    LedgerEntry.category,
                    LedgerEntry.occurred_at,
                ).where(
                    self._entry_scope(user_open_id),
                    LedgerEntry.deleted_at.is_(None),
                    LedgerEntry.occurred_at >= start,
                    LedgerEntry.occurred_at < end,
                )
            )
        ).all()
        income = Decimal("0")
        expense = Decimal("0")
        daily: defaultdict[date, list[Decimal]] = defaultdict(
            lambda: [Decimal("0"), Decimal("0")]
        )
        monthly: defaultdict[str, list[Decimal]] = defaultdict(
            lambda: [Decimal("0"), Decimal("0")]
        )
        categories: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for amount_value, direction, category, occurred_at in rows:
            amount = Decimal(amount_value)
            local = self._local(occurred_at)
            period = local.date()
            month = period.strftime("%Y-%m")
            if direction is Direction.INCOME:
                income += amount
                daily[period][0] += amount
                monthly[month][0] += amount
            else:
                expense += amount
                daily[period][1] += amount
                monthly[month][1] += amount
                categories[str(category)] += amount
        trend = []
        current = start_date
        while current <= end_date:
            day_income, day_expense = daily[current]
            trend.append(
                AnalyticsTrendPoint(
                    period=current,
                    income=day_income,
                    expense=day_expense,
                    balance=day_income - day_expense,
                )
            )
            current += timedelta(days=1)
        category_rows = [
            AnalyticsCategory(
                category=category,
                amount=amount,
                ratio=(amount / expense * 100 if expense else Decimal("0")),
            )
            for category, amount in sorted(
                categories.items(), key=lambda item: item[1], reverse=True
            )
        ]
        month_cursor = start_date.replace(day=1)
        monthly_rows: list[AnalyticsMonthlyPoint] = []
        while month_cursor <= end_date:
            key = month_cursor.strftime("%Y-%m")
            month_income, month_expense = monthly[key]
            monthly_rows.append(
                AnalyticsMonthlyPoint(
                    period=key,
                    income=month_income,
                    expense=month_expense,
                    balance=month_income - month_expense,
                )
            )
            month_cursor = (
                month_cursor.replace(year=month_cursor.year + 1, month=1)
                if month_cursor.month == 12
                else month_cursor.replace(month=month_cursor.month + 1)
            )
        return (
            AnalyticsSummary(
                range_start=start,
                range_end=end,
                income=income,
                expense=expense,
                balance=income - expense,
                entry_count=len(rows),
            ),
            trend,
            category_rows,
            monthly_rows,
        )

    async def budgets(
        self, user_open_id: RequestContext | str, *, now: datetime | None = None
    ) -> BudgetOverview:
        current = self._local(now or datetime.now(UTC))
        month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        )
        budgets = (
            await self._session.scalars(
                select(CategoryBudget)
                .where(self._budget_scope(user_open_id))
                .order_by(CategoryBudget.category)
            )
        ).all()
        expenses = (
            await self._session.execute(
                select(LedgerEntry.category, LedgerEntry.amount).where(
                    self._entry_scope(user_open_id),
                    LedgerEntry.direction == Direction.EXPENSE,
                    LedgerEntry.deleted_at.is_(None),
                    LedgerEntry.occurred_at >= month_start.astimezone(UTC),
                    LedgerEntry.occurred_at < month_end.astimezone(UTC),
                )
            )
        ).all()
        spent_by_category: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for category, amount in expenses:
            spent_by_category[str(category)] += Decimal(amount)
        items: list[BudgetItem] = []
        for budget in budgets:
            spent = spent_by_category[budget.category]
            items.append(
                BudgetItem(
                    category=budget.category,
                    amount=budget.amount,
                    spent=spent,
                    remaining=budget.amount - spent,
                    usage_rate=spent / budget.amount * 100,
                )
            )
        total_budget = sum((item.amount for item in items), Decimal("0"))
        total_spent = sum((item.spent for item in items), Decimal("0"))
        return BudgetOverview(
            currency=self._currency,
            total_budget=total_budget,
            total_spent=total_spent,
            total_remaining=total_budget - total_spent,
            usage_rate=(
                total_spent / total_budget * 100 if total_budget else Decimal("0")
            ),
            items=items,
        )

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
    def _budget_scope(scope: RequestContext | str) -> Any:
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
