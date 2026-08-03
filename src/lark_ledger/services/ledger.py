from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import (
    Action,
    CategoryTotal,
    ExecutionResult,
    ParsedCommand,
    ReportData,
    TrendPoint,
)

HELP_TEXT = (
    "我可以帮你记账、修改、撤销、汇总和生成消费报告。试试：\n"
    "• 午饭32\n• 昨天打车38.5\n• 工资到账10000\n"
    "• 上一笔改成8块\n• 这个月餐饮花了多少\n• 撤销刚才那笔"
    "\n• 生成这个月的消费图表"
)


class LedgerService:
    def __init__(
        self, session: AsyncSession, currency: str = "CNY", timezone: str = "Asia/Shanghai"
    ) -> None:
        self.session = session
        self.currency = currency
        self.timezone = ZoneInfo(timezone)

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
        entry = LedgerEntry(
            user_open_id=user_open_id,
            amount=command.amount,
            currency=self.currency,
            direction=command.direction,
            category=command.category,
            note=command.note or "",
            occurred_at=command.occurred_at,
            source_type=source_type,
            source_message_id=source_message_id,
        )
        self.session.add(entry)
        await self.session.commit()
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        note = f"（{entry.note}）" if entry.note else ""
        return ExecutionResult(
            message=f"已记录{sign} ¥{entry.amount:.2f} · {entry.category}{note}"
        )

    async def _update_last(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        entry = (await self.session.execute(self._latest_query(user_open_id))).scalar_one_or_none()
        if entry is None:
            return ExecutionResult(message="还没有可以修改的记录。")
        for field in ("amount", "direction", "category", "note", "occurred_at"):
            value = getattr(command, field)
            if value is not None:
                setattr(entry, field, value)
        await self.session.commit()
        return ExecutionResult(message=f"已修改上一笔：¥{entry.amount:.2f} · {entry.category}")

    async def _undo_last(self, user_open_id: str) -> ExecutionResult:
        entry = (await self.session.execute(self._latest_query(user_open_id))).scalar_one_or_none()
        if entry is None:
            return ExecutionResult(message="还没有可以撤销的记录。")
        entry.deleted_at = datetime.now(UTC)
        await self.session.commit()
        return ExecutionResult(message=f"已撤销：¥{entry.amount:.2f} · {entry.category}")

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
