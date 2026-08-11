"""Deterministic financial insights (P33-B).

``InsightService.insights`` discovers facts worth surfacing from the live
ledger using **only** deterministic rules — no AI ever computes a number here,
and no AI ever reads the database. The pipeline is:

    Ledger Data
      → Metric / Rule Engine (this module)
      → Structured ``Insight``
      → Presentation (Web / Feishu)
      → Optional AI explanation (separate layer, consumes only the structured
        insight and falls back to this summary on any failure)

v0.8.0 ships exactly four insight types:

* ``spending_change`` (I01) — this month's category spend vs the trailing
  3-month average; transfer / income / pending / deleted are excluded and
  private data is filtered by ``PrivacyService`` per actor.
* ``budget_risk`` (I02) — budget usage rate strictly ahead of the elapsed
  period ratio plus a policy margin; it states "超支风险", never a prediction.
* ``upcoming_recurring`` (I03) — active expense rules due within 30 days,
  privacy-filtered, grouped by currency (different currencies are never added).
* ``goal_progress`` (I04) — goal reached / deadline within 30 days / projected
  shortfall from the deterministic trailing-rate forecast.

Insufficient data yields ``[]`` — no insight is forced from noise. Thresholds
live in one ``InsightPolicy``, not scattered constants.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Direction,
    GoalStatus,
    LedgerEntry,
    RecurringRule,
    RecurringRuleStatus,
)
from lark_ledger.services.budget import BudgetService, normalize_period, period_key
from lark_ledger.services.goals import GoalProgressService, GoalService
from lark_ledger.services.privacy import PrivacyService
from lark_ledger.web_schemas import Insight

MAX_MONEY = Decimal("999999999999.99")


@dataclass(frozen=True, slots=True)
class InsightPolicy:
    """Single place for every insight threshold (P33 §24)."""

    # I01 — a category change fires only when the current month exceeds the
    # trailing average by at least this ratio (30%) and the absolute jump is at
    # least ``minimum_change_amount``.
    spending_change_threshold: Decimal = Decimal("0.30")
    minimum_change_amount: Decimal = Decimal("0")
    # I01 — history must reach back at least this far, else the average is not
    # a meaningful baseline and no change insight is produced.
    minimum_history_days: int = 30
    # I01 — months of history averaged for the baseline.
    history_months: int = 3
    # I02 — risk fires when usage > elapsed_ratio + margin.
    budget_risk_margin: Decimal = Decimal("0.15")
    max_budget_items: int = 3
    # I03 — window for upcoming recurring rules.
    upcoming_recurring_days: int = 30
    # I04 — deadline within this many days becomes an attention insight.
    goal_soon_days: int = 30
    # I04 — deterministic forecast window (days) for the net-saving rate.
    forecast_history_days: int = 90
    # Overall cap returned by one ``insights`` call.
    max_insights: int = 5


class InsightService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        timezone: str,
        currency: str,
        policy: InsightPolicy | None = None,
    ) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency
        self._policy = policy or InsightPolicy()

    async def insights(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[Insight]:
        """Compute the current ledger's insights deterministically.

        Every rule is ledger-scoped first (``ledger_id``) and privacy-filtered
        second; a household member never sees another member's private data in
        any insight, including side channels (category totals, budget spend,
        member statistics). Returns ``[]`` when nothing is worth surfacing.
        """
        current = (now or datetime.now(UTC)).astimezone(self._timezone)
        today = current.date()
        target = normalize_period(period or today)
        candidates: list[Insight] = []
        candidates.extend(await self._spending_change(context, target, today))
        candidates.extend(await self._budget_risk(context, target, today))
        candidates.extend(await self._upcoming_recurring(context, today))
        candidates.extend(await self._goal_progress(context, today, now))
        candidates.sort(key=lambda item: (self._severity_rank(item.severity), item.key))
        cap = limit if limit is not None else self._policy.max_insights
        return candidates[: max(0, min(cap, self._policy.max_insights))]

    # -- I01 spending change ----------------------------------------------

    async def _spending_change(
        self, context: RequestContext, period: date, today: date
    ) -> list[Insight]:
        policy = self._policy
        current_start, current_end = self._period_bounds(period)
        history_start, _ = self._period_bounds(self._shift_months(period, -policy.history_months))

        privacy = await PrivacyService(self._session).entry_visibility_scope(context)
        base_filters: list[Any] = [
            LedgerEntry.ledger_id == context.ledger_id,
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.direction == Direction.EXPENSE,
        ]
        if privacy is not None:
            base_filters.append(privacy)

        # History reach check: the earliest expense in the history window must
        # predate today by at least ``minimum_history_days``.
        earliest = await self._session.scalar(
            select(func.min(LedgerEntry.occurred_at)).where(
                *base_filters,
                LedgerEntry.occurred_at >= history_start,
                LedgerEntry.occurred_at < current_start,
            )
        )
        history_available = False
        if earliest is not None:
            earliest_local = earliest.astimezone(self._timezone).date()
            if (today - earliest_local).days >= policy.minimum_history_days:
                history_available = True

        current_totals = await self._month_totals(base_filters, current_start, current_end)
        if not current_totals:
            return []
        insights: list[Insight] = []
        # No meaningful history → no baseline comparison and no forced insight:
        # a ledger with a few days of data does not need spending-change noise.
        if not history_available:
            return []

        baseline_totals = await self._month_totals(base_filters, history_start, current_start)
        baseline_monthly = {
            category: total / Decimal(policy.history_months)
            for category, total in baseline_totals.items()
        }
        for category, current_total in sorted(
            current_totals.items(), key=lambda item: item[1], reverse=True
        ):
            baseline = baseline_monthly.get(category, Decimal("0"))
            if baseline <= 0:
                # Zero-baseline rule: brand-new spending is surfaced as info
                # (never division by zero, never 999999%).
                if current_total < policy.minimum_change_amount:
                    continue
                insights.append(
                    Insight(
                        key=f"spending_new:{category}:{period_key(period)}",
                        type="spending_change",
                        severity="info",
                        title="新的支出类别",
                        summary=f"本月出现新的{category}支出 {self._money(current_total)}",
                        metric={
                            "category": category,
                            "current": self._fmt(current_total),
                            "baseline": "0",
                            "change": self._fmt(current_total),
                            "change_percent": "",
                        },
                        period=period_key(period),
                        related_category=category,
                        generated_at=datetime.now(UTC),
                    )
                )
                continue
            change = current_total - baseline
            if change <= 0:
                continue
            change_ratio = change / baseline
            if (
                change_ratio < policy.spending_change_threshold
                or change < policy.minimum_change_amount
            ):
                continue
            percent = (change_ratio * 100).quantize(Decimal("0.1"))
            insights.append(
                Insight(
                    key=f"spending_change:{category}:{period_key(period)}",
                    type="spending_change",
                    severity="attention",
                    title="支出明显上升",
                    summary=(
                        f"本月{category}支出 {self._money(current_total)}，"
                        f"近 {policy.history_months} 个月平均 {self._money(baseline)}，"
                        f"增加 {percent}%"
                    ),
                    metric={
                        "category": category,
                        "current": self._fmt(current_total),
                        "baseline": self._fmt(baseline),
                        "change": self._fmt(change),
                        "change_percent": self._fmt(percent),
                    },
                    period=period_key(period),
                    related_category=category,
                    generated_at=datetime.now(UTC),
                )
            )
        return insights

    async def _month_totals(
        self, base_filters: list[Any], start: datetime, end: datetime
    ) -> dict[str, Decimal]:
        """Sum expense amounts per category in ``[start, end)``."""
        rows = (
            await self._session.execute(
                select(LedgerEntry.category, func.sum(LedgerEntry.amount).label("total")).where(
                    *base_filters, LedgerEntry.occurred_at >= start, LedgerEntry.occurred_at < end
                ).group_by(LedgerEntry.category)
            )
        ).all()
        return {str(category): Decimal(total) for category, total in rows}

    # -- I02 budget risk ---------------------------------------------------

    async def _budget_risk(
        self, context: RequestContext, period: date, today: date
    ) -> list[Insight]:
        policy = self._policy
        elapsed = Decimal(today.day) / Decimal(monthrange(today.year, today.month)[1])
        elapsed_percent = (elapsed * 100).quantize(Decimal("0.01"))
        overview = await BudgetService(
            self._session, currency=self._currency, timezone=str(self._timezone)
        ).overview(context, period=period)
        risky: list[tuple[str, Decimal, Decimal]] = []
        spent_by_category = {item.category: item.spent for item in overview.items}
        budget_by_category = {item.category: item.amount for item in overview.items}
        for item in overview.items:
            if item.amount is None or item.usage_rate is None:
                continue
            if item.amount <= 0:
                continue
            margin_limit = (elapsed + policy.budget_risk_margin) * 100
            if item.usage_rate > margin_limit:
                risky.append(
                    (
                        item.category,
                        item.usage_rate,
                        spent_by_category.get(item.category, Decimal("0")),
                    )
                )
        risky.sort(key=lambda row: row[1], reverse=True)
        insights: list[Insight] = []
        for category, usage_rate, spent in risky[: policy.max_budget_items]:
            budget_amount = budget_by_category.get(category, Decimal("0")) or Decimal("0")
            insights.append(
                Insight(
                    key=f"budget_risk:{category}:{period_key(period)}",
                    type="budget_risk",
                    severity="warning",
                    title="预算使用速度偏快",
                    summary=(
                        f"本月{category}预算已使用 {self._fmt(usage_rate)}%，"
                        f"而时间才过去 {self._fmt(elapsed_percent)}%。"
                        "按当前使用速度，预算存在超支风险。"
                    ),
                    metric={
                        "category": category,
                        "usage_rate": self._fmt(usage_rate),
                        "elapsed_ratio": self._fmt(elapsed_percent),
                        "budget": self._fmt(budget_amount),
                        "spent": self._fmt(spent),
                    },
                    period=period_key(period),
                    related_category=category,
                    generated_at=datetime.now(UTC),
                )
            )
        return insights

    # -- I03 upcoming recurring --------------------------------------------

    async def _upcoming_recurring(
        self, context: RequestContext, today: date
    ) -> list[Insight]:
        policy = self._policy
        horizon = today + timedelta(days=policy.upcoming_recurring_days)
        filters: list[Any] = [
            RecurringRule.ledger_id == context.ledger_id,
            RecurringRule.status == RecurringRuleStatus.ACTIVE.value,
            RecurringRule.next_occurrence >= today,
            RecurringRule.next_occurrence <= horizon,
            RecurringRule.transaction_type == Direction.EXPENSE,
        ]
        privacy = PrivacyService(self._session)
        if await privacy.privacy_enabled(context):
            filters.append(
                RecurringRule.account_id.is_(None)
                | privacy.account_visible_exists(context, RecurringRule.account_id)
            )
        rows = list(
            (
                await self._session.scalars(
                    select(RecurringRule).where(*filters).order_by(RecurringRule.next_occurrence)
                )
            ).all()
        )
        if not rows:
            return []
        totals: dict[str, Decimal] = {}
        count = 0
        for rule in rows:
            totals[rule.currency] = totals.get(rule.currency, Decimal("0")) + rule.amount
            count += 1
        by_currency = "、".join(
            f"{self._money(amount)}{currency}" for currency, amount in sorted(totals.items())
        )
        return [
            Insight(
                key=f"upcoming_recurring:{period_key(today)}",
                type="upcoming_recurring",
                severity="info",
                title="未来周期支出",
                summary=(
                    f"未来 {policy.upcoming_recurring_days} 天有 {count} 笔周期支出，"
                    f"合计 {by_currency}"
                ),
                metric={
                    "count": str(count),
                    **{
                        f"amount_{currency}": self._fmt(total)
                        for currency, total in totals.items()
                    },
                },
                period=period_key(today),
                generated_at=datetime.now(UTC),
            )
        ]

    # -- I04 goal progress -------------------------------------------------

    async def _goal_progress(
        self, context: RequestContext, today: date, now: datetime | None
    ) -> list[Insight]:
        policy = self._policy
        goals = await GoalService(
            self._session, timezone=str(self._timezone), currency=self._currency
        ).list_goals(context)
        active = [goal for goal in goals if goal.status == GoalStatus.ACTIVE.value]
        if not active:
            return []
        progress_service = GoalProgressService(
            self._session, timezone=str(self._timezone), currency=self._currency
        )
        insights: list[Insight] = []
        for goal in active:
            progress = await progress_service.progress(context, goal, now=now)
            if progress.is_target_reached:
                insights.append(
                    Insight(
                        key=f"goal_reached:{goal.id}",
                        type="goal_progress",
                        severity="info",
                        title="目标已达成",
                        summary=(
                        f"目标「{goal.name}」已达成：{self._money(progress.current_amount)}"
                        f" / {self._money(goal.target_amount)}"
                    ),
                        metric={
                            "goal_id": str(goal.id),
                            "current": self._fmt(progress.current_amount),
                            "target": self._fmt(goal.target_amount),
                            "progress_percent": self._fmt(progress.progress_percent),
                        },
                        period=period_key(today),
                        related_goal=str(goal.id),
                        related_goal_name=goal.name,
                        generated_at=datetime.now(UTC),
                    )
                )
                continue
            shortfall = progress.projected_shortfall_at_target_date
            if shortfall is not None and shortfall > 0:
                insights.append(
                    Insight(
                        key=f"goal_shortfall:{goal.id}",
                        type="goal_progress",
                        severity="warning",
                        title="目标进度落后",
                        summary=(
                            f"目标「{goal.name}」当前 {self._money(progress.current_amount)} / "
                            f"{self._money(goal.target_amount)}（{self._fmt(progress.progress_percent)}%）。"
                            f"按过去 {policy.forecast_history_days} 天储蓄速度，"
                            f"预计到目标日期还差 {self._money(shortfall)}"
                        ),
                        metric={
                            "goal_id": str(goal.id),
                            "current": self._fmt(progress.current_amount),
                            "target": self._fmt(goal.target_amount),
                            "remaining": self._fmt(progress.remaining_amount),
                            "progress_percent": self._fmt(progress.progress_percent),
                            "days_remaining": str(progress.days_remaining or ""),
                            "monthly_saving_rate": self._fmt(
                                progress.monthly_saving_rate or Decimal("0")
                            ),
                            "projected_shortfall": self._fmt(shortfall),
                        },
                        period=period_key(today),
                        related_goal=str(goal.id),
                        related_goal_name=goal.name,
                        generated_at=datetime.now(UTC),
                    )
                )
                continue
            if (
                progress.days_remaining is not None
                and 0 < progress.days_remaining <= policy.goal_soon_days
            ):
                insights.append(
                    Insight(
                        key=f"goal_soon:{goal.id}",
                        type="goal_progress",
                        severity="attention",
                        title="目标临近截止日期",
                        summary=(
                            f"目标「{goal.name}」已完成 {self._fmt(progress.progress_percent)}%，"
                            f"距离目标日期还有 {progress.days_remaining} 天"
                        ),
                        metric={
                            "goal_id": str(goal.id),
                            "current": self._fmt(progress.current_amount),
                            "target": self._fmt(goal.target_amount),
                            "progress_percent": self._fmt(progress.progress_percent),
                            "days_remaining": str(progress.days_remaining),
                        },
                        period=period_key(today),
                        related_goal=str(goal.id),
                        related_goal_name=goal.name,
                        generated_at=datetime.now(UTC),
                    )
                )
        return insights

    # -- helpers -----------------------------------------------------------

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
    def _shift_months(period: date, months: int) -> date:
        total = period.year * 12 + (period.month - 1) + months
        return date(total // 12, total % 12 + 1, 1)

    @staticmethod
    def _severity_rank(severity: str) -> int:
        return {"warning": 0, "attention": 1, "info": 2}.get(severity, 3)

    @staticmethod
    def _fmt(value: Decimal) -> str:
        return format(value, "f")

    @staticmethod
    def _money(value: Decimal) -> str:
        """Compact human money rendering (no currency symbol — the API period
        carries the ledger currency; Feishu/Web add their own formatting)."""
        return f"{value:,.2f}"
