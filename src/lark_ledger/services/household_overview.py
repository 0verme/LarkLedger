"""Deterministic Household Overview (P31).

``HouseholdOverviewService.overview`` is the single backend entry point for the
Web dashboard's "family home" view. Every figure is recomputed from the live
ledger facts with the same口径 as the rest of the app:

* income / expense only by ``direction`` (transfers live in a separate table
  and are never counted as income, expense, or budget usage);
* soft-deleted entries are excluded (``deleted_at IS NULL``);
* pending recurring never counts until its confirmation actually creates an
  entry;
* member contributions aggregate by ``paid_by_user_id``;
* budget progress reuses ``BudgetService``;
* account balances reuse ``TransferService``.

P32 layers account-level privacy on top of every read here by passing a
visibility filter into the shared query services.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    Direction,
    LedgerEntry,
    RecurringRule,
    RecurringRuleStatus,
)
from lark_ledger.services.budget import BudgetService, normalize_period, period_key
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.member_stats import MemberStatsService
from lark_ledger.services.privacy import PrivacyService
from lark_ledger.services.transfers import TransferService
from lark_ledger.services.web_ledger import WebLedgerQueryService
from lark_ledger.web_schemas import (
    AccountBalanceSummary,
    CategoryValue,
    HouseholdOverview,
    OverviewBudget,
    UpcomingRecurringItem,
    WebEntry,
)

TOP_CATEGORIES_LIMIT = 8
UPCOMING_RECURRING_LIMIT = 5
RECENT_TRANSACTIONS_LIMIT = 8


class HouseholdOverviewService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        timezone: str,
        currency: str,
    ) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency
        self._authorization = LedgerAuthorizationService(session)

    async def overview(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        now: datetime | None = None,
        privacy_filter: Any = None,
    ) -> HouseholdOverview:
        """Build the overview for ``context``'s ledger in one deterministic pass.

        ``privacy_filter`` is an optional extra entry-visibility condition added
        by P32 for household ledgers; ``None`` keeps personal-ledger behavior
        unchanged.
        """
        ledger = await self._authorization.get_accessible(
            context.actor_user_id, context.ledger_id
        )
        current = (now or datetime.now(UTC)).astimezone(self._timezone)
        target = normalize_period(period or current.date())
        period_start, period_end = self._period_bounds(target)
        entry_scope = self._entry_scope(context)
        if privacy_filter is None:
            privacy_filter = await PrivacyService(self._session).entry_visibility_scope(
                context
            )

        active_entries = [
            entry_scope,
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.occurred_at >= period_start,
            LedgerEntry.occurred_at < period_end,
        ]
        if privacy_filter is not None:
            active_entries.append(privacy_filter)

        income_total, expense_total = await self._totals(active_entries)
        member_contributions = await MemberStatsService(self._session).stats(
            context, start=period_start, end=period_end, privacy_filter=privacy_filter
        )
        top_categories = await self._top_categories(active_entries, expense_total)
        budget = await self._budget_overview(context, target, now)
        balances = await self._account_balances(context)
        upcoming = await self._upcoming_recurring(context, privacy_filter)
        recent = await self._recent_transactions(context, privacy_filter)

        return HouseholdOverview(
            ledger_id=str(ledger.id),
            ledger_name=ledger.name,
            ledger_kind=ledger.kind,
            period=period_key(target),
            income_total=income_total,
            expense_total=expense_total,
            net_total=income_total - expense_total,
            budget=budget,
            account_balance_summary=balances,
            member_contributions=member_contributions,
            top_categories=top_categories,
            upcoming_recurring=upcoming,
            recent_transactions=recent,
        )

    async def _totals(self, filters: list[Any]) -> tuple[Decimal, Decimal]:
        row = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.INCOME, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.EXPENSE, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).where(*filters)
            )
        ).one()
        return Decimal(row[0]), Decimal(row[1])

    async def _top_categories(
        self, filters: list[Any], expense_total: Decimal
    ) -> list[CategoryValue]:
        rows = (
            await self._session.execute(
                select(LedgerEntry.category, func.sum(LedgerEntry.amount).label("amount"))
                .where(*filters, LedgerEntry.direction == Direction.EXPENSE)
                .group_by(LedgerEntry.category)
                .order_by(func.sum(LedgerEntry.amount).desc())
                .limit(TOP_CATEGORIES_LIMIT)
            )
        ).all()
        return [
            CategoryValue(
                category=str(category),
                amount=Decimal(amount),
                ratio=(
                    Decimal(amount) / expense_total * 100 if expense_total else Decimal("0")
                ),
            )
            for category, amount in rows
        ]

    async def _budget_overview(
        self,
        context: RequestContext,
        period: date,
        now: datetime | None,
    ) -> OverviewBudget:
        overview = await BudgetService(
            self._session, currency=self._currency, timezone=str(self._timezone)
        ).overview(context, period=period, now=now)
        return OverviewBudget(
            total_budget=overview.total_budget,
            total_spent=overview.total_spent,
            total_remaining=overview.total_remaining,
            usage_rate=overview.usage_rate,
            status=overview.status,
        )

    async def _account_balances(self, context: RequestContext) -> AccountBalanceSummary:
        summary = await TransferService(self._session).asset_summary(context)
        return AccountBalanceSummary(
            currency=summary.currency,
            total_assets=summary.total_assets,
            total_liabilities=summary.total_liabilities,
            net_assets=summary.net_assets,
            account_count=len(summary.accounts),
        )

    async def _upcoming_recurring(
        self, context: RequestContext, privacy_filter: Any | None
    ) -> list[UpcomingRecurringItem]:
        today = current_local_date(self._timezone)
        filters: list[Any] = [
            RecurringRule.ledger_id == context.ledger_id,
            RecurringRule.status == RecurringRuleStatus.ACTIVE.value,
            RecurringRule.next_occurrence >= today,
        ]
        if privacy_filter is not None:
            # Privacy filters entries; upcoming rules are filtered by their own
            # account visibility so private-account rules never surface.
            from lark_ledger.services.recurring import RecurringService

            rule_scope = await RecurringService(
                self._session, currency=self._currency, timezone=str(self._timezone)
            )._privacy_rule_scope(context)
            filters.append(rule_scope)
        rules = list(
            (
                await self._session.scalars(
                    select(RecurringRule)
                    .where(*filters)
                    .order_by(RecurringRule.next_occurrence, RecurringRule.created_at)
                    .limit(UPCOMING_RECURRING_LIMIT)
                )
            ).all()
        )
        names = await self._account_names(context.ledger_id, {rule.account_id for rule in rules})
        return [
            UpcomingRecurringItem(
                rule_id=str(rule.id),
                transaction_type=(
                    rule.transaction_type.value
                    if isinstance(rule.transaction_type, Direction)
                    else str(rule.transaction_type)
                ),
                amount=rule.amount,
                currency=rule.currency,
                category=rule.category,
                description=rule.description,
                frequency=rule.frequency,
                next_occurrence=rule.next_occurrence,
                account_name=names.get(rule.account_id),
            )
            for rule in rules
        ]

    async def _recent_transactions(
        self, context: RequestContext, privacy_filter: Any
    ) -> list[WebEntry]:
        filters: list[Any] = [
            self._entry_scope(context),
            LedgerEntry.deleted_at.is_(None),
        ]
        if privacy_filter is not None:
            filters.append(privacy_filter)
        rows = list(
            (
                await self._session.scalars(
                    select(LedgerEntry)
                    .where(*filters)
                    .order_by(
                        LedgerEntry.occurred_at.desc(),
                        LedgerEntry.created_at.desc(),
                        LedgerEntry.id.desc(),
                    )
                    .limit(RECENT_TRANSACTIONS_LIMIT)
                )
            ).all()
        )
        query = WebLedgerQueryService(
            self._session, timezone=str(self._timezone), currency=self._currency
        )
        names = await query._account_names({row.account_id for row in rows})
        payer_names = await query._payer_names(
            context, {row.paid_by_user_id for row in rows}
        )
        return [
            query._entry(
                row,
                names.get(row.account_id),
                payer_names.get(row.paid_by_user_id),
            )
            for row in rows
        ]

    async def _account_names(
        self, ledger_id: uuid.UUID, account_ids: set[Any]
    ) -> dict[Any, str | None]:
        ids = {account_id for account_id in account_ids if account_id is not None}
        if not ids:
            return {}
        rows = (
            await self._session.execute(
                select(Account.id, Account.name).where(
                    Account.ledger_id == ledger_id, Account.id.in_(ids)
                )
            )
        ).all()
        return {account_id: name for account_id, name in rows}

    @staticmethod
    def _entry_scope(context: RequestContext) -> Any:
        from lark_ledger.services.web_ledger import WebLedgerQueryService

        return WebLedgerQueryService._entry_scope(context)

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


def current_local_date(timezone: ZoneInfo) -> date:
    return datetime.now(UTC).astimezone(timezone).date()
