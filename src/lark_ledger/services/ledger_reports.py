"""Internal reporting mixin for ``LedgerService`` (split from ``ledger.py``)."""
# mypy: disable-error-code="attr-defined"

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import (
    DEFAULT_EXPORT_DAYS,
    CategoryTotal,
    ExecutionResult,
    ParsedCommand,
    ReportData,
    TrendPoint,
)
from lark_ledger.services.export import ExportTooLargeError

NOTE_PREVIEW_LEN = 20


class _ReportMixin:
    session: AsyncSession
    currency: str
    timezone: ZoneInfo
    @staticmethod
    def _export_order_by() -> tuple[Any, ...]:
        """Stable chronological order for CSV timeline export."""
        return (
            LedgerEntry.occurred_at.asc(),
            LedgerEntry.created_at.asc(),
            LedgerEntry.id.asc(),
        )

    def _resolve_export_range(
        self, command: ParsedCommand
    ) -> tuple[datetime | None, datetime | None, str]:
        """Return (range_start, range_end, label) with left-closed right-open bounds.

        Defaults to the last DEFAULT_EXPORT_DAYS when neither export_all nor an
        explicit range is provided. AI must not set export_all merely because
        dates are empty.
        """
        if command.export_all:
            return None, None, "全部历史"
        if command.range_start is not None and command.range_end is not None:
            if command.range_start >= command.range_end:
                raise ValueError("export range must be increasing")
            return (
                command.range_start,
                command.range_end,
                self._export_range_label(command.range_start, command.range_end),
            )
        current = self._current_local_datetime()
        start = current - timedelta(days=DEFAULT_EXPORT_DAYS)
        # Exclusive upper bound slightly past "now" so current-moment rows are included.
        end = current + timedelta(seconds=1)
        return start, end, self._export_range_label(start, end)

    def _export_range_label(self, start: datetime, end: datetime) -> str:
        local_start = self._local_datetime(start)
        # end is exclusive; show the last included calendar day in app timezone.
        last_included = self._local_datetime(end) - timedelta(microseconds=1)
        return (
            f"{local_start.strftime('%Y-%m-%d')} 至 "
            f"{last_included.strftime('%Y-%m-%d')}"
        )

    async def _export_entries(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        from lark_ledger.services import ledger as _facade
        try:
            range_start, range_end, range_label = self._resolve_export_range(command)
        except ValueError:
            return ExecutionResult(
                message="导出时间范围无效：开始时间必须早于结束时间。"
            )

        filters: list[Any] = [self._entry_scope(user_open_id)]
        privacy = await self._privacy_entry_filter()
        if privacy is not None:
            filters.append(privacy)
        if not command.include_deleted:
            filters.append(LedgerEntry.deleted_at.is_(None))
        if range_start is not None and range_end is not None:
            filters.append(LedgerEntry.occurred_at >= range_start)
            filters.append(LedgerEntry.occurred_at < range_end)

        # Fetch one extra row to detect over-limit without silent truncation.
        fetched = (
            (
                await self.session.execute(
                    select(LedgerEntry)
                    .where(*filters)
                    .order_by(*self._export_order_by())
                    .limit(_facade.MAX_EXPORT_ROWS + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(fetched) > _facade.MAX_EXPORT_ROWS:
            return ExecutionResult(
                message=(
                    f"符合条件的账目超过 {_facade.MAX_EXPORT_ROWS} 笔，"
                    "请缩小导出时间范围后重试。"
                )
            )
        if not fetched:
            return ExecutionResult(message="该时间范围内没有可导出的账目。")

        try:
            names = await self._account_names(fetched)
            export_file = _facade.build_export_file(
                fetched,
                timezone=self.timezone,
                when=self._current_local_datetime(),
                range_label=range_label,
                account_names=names,
            )
        except ExportTooLargeError:
            return ExecutionResult(
                message="导出文件超过 5MB，请缩小时间范围后重试。"
            )

        return ExecutionResult(
            message=(
                f"已导出 {export_file.row_count} 笔账目，时间范围：{range_label}。"
            ),
            export=export_file,
        )

    @staticmethod
    def _note_preview(note: str) -> str:
        text = note.strip()
        if len(text) > NOTE_PREVIEW_LEN:
            return text[: NOTE_PREVIEW_LEN - 1] + "…"
        return text

    async def _summary(self, user_open_id: str, command: ParsedCommand) -> ExecutionResult:
        assert command.range_start is not None
        assert command.range_end is not None
        filters = [
            self._entry_scope(user_open_id),
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.occurred_at >= command.range_start,
            LedgerEntry.occurred_at < command.range_end,
        ]
        privacy = await self._privacy_entry_filter()
        if privacy is not None:
            filters.append(privacy)
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

        filters: list[Any] = [
            self._entry_scope(user_open_id),
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.occurred_at >= command.range_start,
            LedgerEntry.occurred_at < command.range_end,
        ]
        privacy = await self._privacy_entry_filter()
        if privacy is not None:
            filters.append(privacy)
        rows = (
            await self.session.execute(
                select(
                    LedgerEntry.amount,
                    LedgerEntry.direction,
                    LedgerEntry.category,
                    LedgerEntry.occurred_at,
                ).where(*filters)
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
