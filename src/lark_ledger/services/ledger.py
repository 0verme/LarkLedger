import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import BudgetAlert, CategoryBudget, Direction, LedgerEntry
from lark_ledger.schemas import (
    SUPPORTED_INPUT_CURRENCIES,
    Action,
    BudgetCandidate,
    CategoryTotal,
    ExecutionResult,
    ParsedCommand,
    ReportData,
    TrendPoint,
)
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError

MAX_MONEY = Decimal("999999999999.99")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ConvertedAmount:
    amount: Decimal
    original_amount: Decimal
    original_currency: str

    @property
    def was_converted(self) -> bool:
        return self.original_currency != ""

HELP_TEXT = (
    "我可以帮你记账、修改、撤销、汇总、设置预算和生成消费报告。试试：\n"
    "• 午饭32\n• 昨天打车38.5\n• 工资到账10000\n"
    "• 上一笔改成8块\n• 这个月餐饮花了多少\n• 撤销刚才那笔"
    "\n• 交通预算500，人情往来预算1000\n• 查看预算\n• 生成这个月的消费图表"
)


class LedgerService:
    def __init__(
        self,
        session: AsyncSession,
        currency: str = "CNY",
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
        exchange_rates: ExchangeRateService | None = None,
    ) -> None:
        self.session = session
        self.currency = currency
        self.timezone = ZoneInfo(timezone)
        self.now = now
        self.exchange_rates = exchange_rates

    async def execute(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str = "text",
        source_message_id: str | None = None,
    ) -> ExecutionResult:
        if command.action is Action.CREATE:
            return await self._create(
                user_open_id, command, source_type=source_type, source_message_id=source_message_id
            )
        if command.action is Action.UPDATE_LAST:
            return await self._update_last(user_open_id, command)
        if command.action is Action.UNDO_LAST:
            return await self._undo_last(user_open_id)
        if command.action is Action.SUMMARY:
            return await self._summary(user_open_id, command)
        if command.action is Action.REPORT:
            return await self._report(user_open_id, command)
        if command.action is Action.SET_BUDGET:
            return await self._set_budget(user_open_id, command)
        if command.action is Action.SET_BUDGETS:
            return await self._set_budgets(user_open_id, command)
        if command.action is Action.LIST_BUDGETS:
            return await self._list_budgets(user_open_id, command)
        if command.action is Action.DELETE_BUDGET:
            return await self._delete_budget(user_open_id, command)
        return ExecutionResult(message=HELP_TEXT)

    def _latest_query(self, user_open_id: str) -> Select[tuple[LedgerEntry]]:
        return (
            select(LedgerEntry)
            .where(LedgerEntry.user_open_id == user_open_id, LedgerEntry.deleted_at.is_(None))
            .order_by(LedgerEntry.occurred_at.desc(), LedgerEntry.created_at.desc())
            .limit(1)
        )

    async def _create(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None,
    ) -> ExecutionResult:
        assert command.amount is not None
        assert command.direction is not None
        assert command.category is not None
        assert command.occurred_at is not None
        converted = await self._convert_command_amount(command)
        entry = LedgerEntry(
            user_open_id=user_open_id,
            amount=converted.amount,
            currency=self.currency,
            direction=command.direction,
            category=command.category,
            note=command.note or "",
            occurred_at=command.occurred_at,
            source_type=source_type,
            source_message_id=source_message_id,
        )
        self.session.add(entry)
        budget_alert = await self._check_budget(entry)
        await self.session.commit()
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        note = f"（{entry.note}）" if entry.note else ""
        conversion = self._conversion_note(converted)
        return ExecutionResult(
            message=(
                f"已记录{sign} {self._format_money(entry.amount)}{conversion}"
                f" · {entry.category}{note}"
            ),
            budget_alert=budget_alert,
        )

    async def _update_last(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        entry = (await self.session.execute(self._latest_query(user_open_id))).scalar_one_or_none()
        if entry is None:
            return ExecutionResult(message="还没有可以修改的记录。")
        converted = (
            await self._convert_command_amount(command) if command.amount is not None else None
        )
        if converted is not None:
            entry.amount = converted.amount
        for field in ("direction", "category", "note", "occurred_at"):
            value = getattr(command, field)
            if value is not None:
                setattr(entry, field, value)
        budget_alert = await self._check_budget(entry)
        await self.session.commit()
        conversion = self._conversion_note(converted) if converted is not None else ""
        return ExecutionResult(
            message=(
                f"已修改上一笔：{self._format_money(entry.amount)}{conversion} · {entry.category}"
            ),
            budget_alert=budget_alert,
        )

    async def _undo_last(self, user_open_id: str) -> ExecutionResult:
        entry = (await self.session.execute(self._latest_query(user_open_id))).scalar_one_or_none()
        if entry is None:
            return ExecutionResult(message="还没有可以撤销的记录。")
        entry.deleted_at = datetime.now(UTC)
        await self.session.commit()
        return ExecutionResult(message=f"已撤销：¥{entry.amount:.2f} · {entry.category}")

    async def _set_budget(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.amount is not None
        assert command.category is not None
        converted = await self._convert_command_amount(command)
        budget = await self._budget_for_update(user_open_id, command.category)
        if budget is None:
            budget = CategoryBudget(
                user_open_id=user_open_id,
                category=command.category,
                amount=converted.amount,
            )
            self.session.add(budget)
        else:
            budget.amount = converted.amount
        await self.session.flush()
        spent = await self._monthly_spend(user_open_id, command.category)
        await self.session.commit()
        progress = self._budget_progress(spent, converted.amount)
        conversion = self._conversion_note(converted)
        return ExecutionResult(
            message=(
                f"已设置每月{command.category}预算 "
                f"{self._format_money(converted.amount)}{conversion}\n"
                f"本月已用 ¥{spent:.2f} · {progress}"
            )
        )

    async def _set_budgets(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.budgets is not None
        last_indexes: dict[str, int] = {}
        for index, candidate in enumerate(command.budgets):
            category = (candidate.category or "").strip()
            if category:
                last_indexes[category] = index

        successes: list[str] = []
        failures: list[str] = []
        for index, candidate in enumerate(command.budgets):
            category = (candidate.category or "").strip()
            if category and last_indexes[category] != index:
                continue
            item_command, error = self._validated_budget_candidate(candidate)
            label = category or f"第 {index + 1} 项"
            if item_command is None:
                failures.append(f"❌ {label}：{error}")
                continue
            try:
                result = await self._set_budget(user_open_id, item_command)
            except ExchangeRateUnavailableError:
                await self.session.rollback()
                failures.append(f"❌ {label}：暂时无法获取汇率")
            except ValueError:
                await self.session.rollback()
                failures.append(f"❌ {label}：换算后的金额超出支持范围")
            except Exception:
                await self.session.rollback()
                logger.exception("failed to persist batch budget item %s", index + 1)
                failures.append(f"❌ {label}：保存失败，请稍后重试")
            else:
                successes.append("✅ " + result.message.replace("\n", "；"))

        lines = [
            f"批量预算处理完成：成功 {len(successes)} 项，失败 {len(failures)} 项",
            *successes,
            *failures,
        ]
        return ExecutionResult(message="\n".join(lines))

    @staticmethod
    def _validated_budget_candidate(
        candidate: BudgetCandidate,
    ) -> tuple[ParsedCommand | None, str | None]:
        category = (candidate.category or "").strip()
        if not category:
            return None, "缺少分类"
        if len(category) > 64:
            return None, "分类名称过长"
        if candidate.amount is None or (
            isinstance(candidate.amount, str) and not candidate.amount.strip()
        ):
            return None, "缺少金额"
        currency = (candidate.currency or "").strip().upper() or None
        if currency is not None and currency not in SUPPORTED_INPUT_CURRENCIES:
            return None, f"不支持币种 {currency[:12]}"
        try:
            item = ParsedCommand(
                action=Action.SET_BUDGET,
                amount=candidate.amount,
                currency=currency,
                category=category,
            )
        except ValidationError:
            return None, "金额格式无效或不在支持范围内"
        return item, None

    async def _convert_command_amount(self, command: ParsedCommand) -> _ConvertedAmount:
        assert command.amount is not None
        source = command.currency or self.currency
        was_converted = source != self.currency
        if was_converted:
            if self.exchange_rates is None:
                raise RuntimeError("exchange rate service is not configured")
            amount = await self.exchange_rates.convert(command.amount, source, self.currency)
        else:
            amount = command.amount
        if amount <= 0:
            raise ValueError("converted amount must be at least 0.01")
        if amount > MAX_MONEY:
            raise ValueError("converted amount exceeds the ledger field limit")
        return _ConvertedAmount(
            amount=amount,
            original_amount=command.amount,
            original_currency=source if was_converted else "",
        )

    def _format_money(self, amount: Decimal) -> str:
        if self.currency == "CNY":
            return f"¥{amount:.2f}"
        return f"{amount:.2f} {self.currency}"

    @staticmethod
    def _conversion_note(converted: _ConvertedAmount) -> str:
        if not converted.was_converted:
            return ""
        return (
            f"（由 {converted.original_amount:.2f} {converted.original_currency} 约算）"
        )

    async def _list_budgets(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        query = select(CategoryBudget).where(CategoryBudget.user_open_id == user_open_id)
        if command.category:
            query = query.where(CategoryBudget.category == command.category)
        budgets = (
            (await self.session.execute(query.order_by(CategoryBudget.category))).scalars().all()
        )
        if not budgets:
            if command.category:
                return ExecutionResult(message=f"还没有设置{command.category}月预算。")
            return ExecutionResult(message="还没有设置任何月预算。")
        lines: list[str] = []
        for budget in budgets:
            spent = await self._monthly_spend(user_open_id, budget.category)
            lines.append(
                f"• {budget.category}：¥{spent:.2f} / ¥{budget.amount:.2f}"
                f"（{self._budget_progress(spent, budget.amount)}）"
            )
        return ExecutionResult(message="本月预算进度\n" + "\n".join(lines))

    async def _delete_budget(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.category is not None
        budget = await self._budget_for_update(user_open_id, command.category)
        if budget is None:
            return ExecutionResult(message=f"还没有设置{command.category}月预算。")
        await self.session.execute(delete(BudgetAlert).where(BudgetAlert.budget_id == budget.id))
        await self.session.delete(budget)
        await self.session.commit()
        return ExecutionResult(message=f"已取消{command.category}月预算。")

    async def _check_budget(self, entry: LedgerEntry) -> str | None:
        if entry.direction is not Direction.EXPENSE or entry.deleted_at is not None:
            return None
        current = self._current_local_datetime()
        occurred = self._local_datetime(entry.occurred_at)
        if (occurred.year, occurred.month) != (current.year, current.month):
            return None

        with self.session.no_autoflush:
            budget = await self._budget_for_update(entry.user_open_id, entry.category)
        if budget is None:
            return None
        await self.session.flush()
        spent = await self._monthly_spend(entry.user_open_id, entry.category)
        existing = set(
            (
                await self.session.execute(
                    select(BudgetAlert.threshold).where(
                        BudgetAlert.budget_id == budget.id,
                        BudgetAlert.period_start == current.date().replace(day=1),
                    )
                )
            ).scalars()
        )
        period_start = current.date().replace(day=1)
        if spent >= budget.amount:
            for threshold in (80, 100):
                if threshold not in existing:
                    self.session.add(
                        BudgetAlert(
                            budget_id=budget.id,
                            period_start=period_start,
                            threshold=threshold,
                        )
                    )
            if 100 in existing:
                return None
            exceeded = spent - budget.amount
            return (
                f"⚠️ {entry.category}本月预算已超额\n"
                f"预算 ¥{budget.amount:.2f} · 已用 ¥{spent:.2f} · "
                f"超出 ¥{exceeded:.2f}（{self._usage_percent(spent, budget.amount)}）"
            )
        if spent * 100 >= budget.amount * 80 and 80 not in existing:
            self.session.add(
                BudgetAlert(
                    budget_id=budget.id,
                    period_start=period_start,
                    threshold=80,
                )
            )
            remaining = budget.amount - spent
            return (
                f"⏰ {entry.category}本月预算快用完了\n"
                f"预算 ¥{budget.amount:.2f} · 已用 ¥{spent:.2f} · "
                f"剩余 ¥{remaining:.2f}（{self._usage_percent(spent, budget.amount)}）"
            )
        return None

    async def _budget_for_update(
        self, user_open_id: str, category: str
    ) -> CategoryBudget | None:
        return (
            await self.session.execute(
                select(CategoryBudget)
                .where(
                    CategoryBudget.user_open_id == user_open_id,
                    CategoryBudget.category == category,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _monthly_spend(self, user_open_id: str, category: str) -> Decimal:
        start, end = self._current_month_bounds()
        amount = await self.session.scalar(
            select(func.sum(LedgerEntry.amount)).where(
                LedgerEntry.user_open_id == user_open_id,
                LedgerEntry.category == category,
                LedgerEntry.direction == Direction.EXPENSE,
                LedgerEntry.deleted_at.is_(None),
                LedgerEntry.occurred_at >= start,
                LedgerEntry.occurred_at < end,
            )
        )
        return Decimal(amount or 0)

    def _current_local_datetime(self) -> datetime:
        return self._local_datetime(self.now or datetime.now(UTC))

    def _current_month_bounds(self) -> tuple[datetime, datetime]:
        current = self._current_local_datetime()
        start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start.astimezone(UTC), end.astimezone(UTC)

    @classmethod
    def _budget_progress(cls, spent: Decimal, budget: Decimal) -> str:
        if spent > budget:
            return f"已超出 ¥{spent - budget:.2f}"
        return f"已用 {cls._usage_percent(spent, budget)}，剩余 ¥{budget - spent:.2f}"

    @staticmethod
    def _usage_percent(spent: Decimal, budget: Decimal) -> str:
        return f"{spent / budget * 100:.0f}%"

    async def _summary(self, user_open_id: str, command: ParsedCommand) -> ExecutionResult:
        assert command.range_start is not None
        assert command.range_end is not None
        filters = [
            LedgerEntry.user_open_id == user_open_id,
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.occurred_at >= command.range_start,
            LedgerEntry.occurred_at < command.range_end,
        ]
        filters.append(
            LedgerEntry.direction == (command.direction or Direction.EXPENSE)
        )
        if command.category:
            filters.append(LedgerEntry.category == command.category)
        rows = (
            await self.session.execute(
                select(LedgerEntry.category, func.sum(LedgerEntry.amount))
                .where(*filters)
                .group_by(LedgerEntry.category)
                .order_by(func.sum(LedgerEntry.amount).desc())
            )
        ).all()
        if not rows:
            return ExecutionResult(message="这个时间范围内没有找到记录。")
        total = sum((Decimal(amount) for _, amount in rows), Decimal("0"))
        details = "\n".join(f"• {category}：¥{Decimal(amount):.2f}" for category, amount in rows)
        kind = "收入" if command.direction is Direction.INCOME else "支出"
        return ExecutionResult(message=f"合计{kind} ¥{total:.2f}\n{details}")

    async def _report(self, user_open_id: str, command: ParsedCommand) -> ExecutionResult:
        assert command.range_start is not None
        assert command.range_end is not None
        local_start = self._local_datetime(command.range_start)
        local_end = self._local_datetime(command.range_end)
        if local_end - local_start > timedelta(days=366):
            return ExecutionResult(message="单份消费报告最长支持 366 天，请缩短时间范围后重试。")

        rows = (
            await self.session.execute(
                select(
                    LedgerEntry.amount,
                    LedgerEntry.direction,
                    LedgerEntry.category,
                    LedgerEntry.occurred_at,
                ).where(
                    LedgerEntry.user_open_id == user_open_id,
                    LedgerEntry.deleted_at.is_(None),
                    LedgerEntry.occurred_at >= command.range_start,
                    LedgerEntry.occurred_at < command.range_end,
                )
            )
        ).all()
        if not rows:
            return ExecutionResult(message="该时间范围暂无记录。")

        income_total = Decimal("0")
        expense_total = Decimal("0")
        categories: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        use_daily = local_end - local_start <= timedelta(days=92)
        trend: defaultdict[date, Decimal] = defaultdict(lambda: Decimal("0"))

        for amount_value, direction, category, occurred_at in rows:
            amount = Decimal(amount_value)
            if direction is Direction.INCOME:
                income_total += amount
                continue
            expense_total += amount
            categories[str(category)] += amount
            local_date = self._local_datetime(occurred_at).date()
            bucket = local_date if use_daily else local_date.replace(day=1)
            trend[bucket] += amount

        trend_points: list[TrendPoint] = []
        if expense_total > 0:
            periods = self._periods(local_start.date(), local_end.date(), use_daily)
            trend_points = [TrendPoint(period=period, amount=trend[period]) for period in periods]

        report = ReportData(
            range_start=command.range_start,
            range_end=command.range_end,
            currency=self.currency,
            income_total=income_total,
            expense_total=expense_total,
            balance=income_total - expense_total,
            entry_count=len(rows),
            categories=[
                CategoryTotal(category=category, amount=amount)
                for category, amount in sorted(
                    categories.items(), key=lambda item: item[1], reverse=True
                )
            ],
            trend=trend_points,
            trend_granularity="day" if use_daily else "month",
        )
        return ExecutionResult(
            message=(
                f"收入 ¥{income_total:.2f} · 支出 ¥{expense_total:.2f} · "
                f"结余 ¥{report.balance:.2f}"
            ),
            report=report,
        )

    def _local_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self.timezone)

    @staticmethod
    def _periods(start: date, end: date, daily: bool) -> list[date]:
        periods: list[date] = []
        current = start if daily else start.replace(day=1)
        while current < end:
            periods.append(current)
            if daily:
                current += timedelta(days=1)
            elif current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
        return periods
