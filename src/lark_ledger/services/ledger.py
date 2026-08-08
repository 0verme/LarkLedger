"""Ledger domain facade.

The public import surface is unchanged; the implementation is split into
internal mixins: ``ledger_entries``, ``ledger_budgets`` and ``ledger_reports``.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import CategoryBudget, Ledger, LedgerEntry
from lark_ledger.schemas import (
    MAX_EXPORT_ROWS as MAX_EXPORT_ROWS,  # noqa: F401  # re-exported; tests patch lark_ledger.services.ledger.MAX_EXPORT_ROWS
)
from lark_ledger.schemas import (
    Action,
    ExecutionResult,
    ParsedCommand,
)
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.export import (
    build_export_file as build_export_file,  # noqa: F401  # re-exported; tests patch lark_ledger.services.ledger.build_export_file
)
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger_budgets import _BudgetMixin
from lark_ledger.services.ledger_entries import (
    EntryConflictError as EntryConflictError,  # noqa: F401  # re-exported for existing callers
)
from lark_ledger.services.ledger_entries import (
    _ConvertedAmount,
    _EntryMixin,
)

# NOTE_PREVIEW_LEN re-exported for existing callers
from lark_ledger.services.ledger_reports import (
    NOTE_PREVIEW_LEN as NOTE_PREVIEW_LEN,  # noqa: F401
)
from lark_ledger.services.ledger_reports import (
    _ReportMixin,
)
from lark_ledger.short_id import generate_short_id

MAX_MONEY = Decimal("999999999999.99")


class LedgerAccessDeniedError(PermissionError):
    """The actor is not allowed to operate on the requested ledger."""


HELP_TEXT = (
    "我可以帮你记账、修改、撤销、查看账单、导出 CSV、汇总、设置预算和生成消费报告。试试：\n"
    "• 午饭32\n• 昨天打车38.5\n• 工资到账10000\n"
    "• 早餐12，午饭32，打车45，餐饮预算1000\n"
    "• 最近10笔\n• 查看 #A83F2\n• 把 #A83F2 改成35元\n"
    "• 删除 #A83F2\n• 恢复 #A83F2\n• 上一笔改成8块\n"
    "• 导出本月账单\n• 导出最近90天账单\n• 导出全部账单\n"
    "• 这个月餐饮花了多少\n• 撤销刚才那笔\n"
    "• 交通预算500，人情往来预算1000\n• 查看预算\n• 生成这个月的消费图表"
)


class LedgerService(_EntryMixin, _BudgetMixin, _ReportMixin):
    def __init__(
        self,
        session: AsyncSession,
        currency: str = "CNY",
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
        exchange_rates: ExchangeRateService | None = None,
        short_id_factory: Callable[[], str] | None = None,
        commit_changes: bool = True,
    ) -> None:
        """Ledger operations on ``session``.

        With ``commit_changes=True`` (the default and the legacy behavior) the
        service commits once at the end of ``execute``. The Transactional
        Outbox path (P06a) constructs the service with ``commit_changes=False``
        so the caller owns the transaction: business flushes, reply intents are
        added to the same session, and the caller commits business + outbox
        atomically.
        """
        self.session = session
        self.currency = currency
        self.timezone = ZoneInfo(timezone)
        self.now = now
        self.exchange_rates = exchange_rates
        self._short_id_factory = short_id_factory or generate_short_id
        self.commit_changes = commit_changes
        self._active_context: RequestContext | None = None

    async def execute(
        self,
        context: RequestContext | str,
        command: ParsedCommand,
        *,
        source_type: str = "text",
        source_message_id: str | None = None,
        source_item_index: int = 0,
        expected_updated_at: datetime | None = None,
    ) -> ExecutionResult:
        if isinstance(context, RequestContext):
            self._active_context = context
            user_open_id = context.external_subject_id or str(context.actor_user_id)
        else:
            self._active_context = await IdentityService(
                self.session,
                currency=self.currency,
                timezone=str(self.timezone),
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=context,
            )
            user_open_id = context
        await self._authorize_context()
        if command.action is Action.CREATE:
            result = await self._create(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
                source_item_index=source_item_index,
            )
        elif command.action is Action.BATCH:
            result = await self._execute_batch(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
            )
        elif command.action is Action.CREATE_ENTRIES:
            result = await self._create_entries(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
            )
        elif command.action is Action.UPDATE_LAST:
            result = await self._update_last(user_open_id, command)
        elif command.action is Action.UNDO_LAST:
            result = await self._undo_last(user_open_id)
        elif command.action is Action.LIST_ENTRIES:
            result = await self._list_entries(user_open_id, command)
        elif command.action is Action.GET_ENTRY:
            result = await self._get_entry(user_open_id, command)
        elif command.action is Action.UPDATE_ENTRY:
            result = await self._update_entry(
                user_open_id, command, expected_updated_at=expected_updated_at
            )
        elif command.action is Action.DELETE_ENTRY:
            result = await self._delete_entry(
                user_open_id, command, expected_updated_at=expected_updated_at
            )
        elif command.action is Action.RESTORE_ENTRY:
            result = await self._restore_entry(
                user_open_id, command, expected_updated_at=expected_updated_at
            )
        elif command.action is Action.EXPORT_ENTRIES:
            result = await self._export_entries(user_open_id, command)
        elif command.action is Action.SUMMARY:
            result = await self._summary(user_open_id, command)
        elif command.action is Action.REPORT:
            result = await self._report(user_open_id, command)
        elif command.action is Action.SET_BUDGET:
            result = await self._set_budget(user_open_id, command)
        elif command.action is Action.SET_BUDGETS:
            result = await self._set_budgets(user_open_id, command)
        elif command.action is Action.LIST_BUDGETS:
            result = await self._list_budgets(user_open_id, command)
        elif command.action is Action.DELETE_BUDGET:
            result = await self._delete_budget(user_open_id, command)
        else:
            result = ExecutionResult(message=HELP_TEXT)

        if self.commit_changes:
            await self.session.commit()
        return result

    def _request_context(self) -> RequestContext:
        if self._active_context is None:
            raise RuntimeError("ledger request context is not initialized")
        return self._active_context

    async def _authorize_context(self) -> None:
        context = self._request_context()
        allowed = await self.session.scalar(
            select(Ledger.id).where(
                Ledger.id == context.ledger_id,
                Ledger.owner_user_id == context.actor_user_id,
            )
        )
        if allowed is None:
            raise LedgerAccessDeniedError("actor cannot access the requested ledger")

    def _entry_scope(self, legacy_subject: str) -> Any:
        """Use ledger_id as the authority, with a nullable expand-migration fallback."""

        return or_(
            LedgerEntry.ledger_id == self._request_context().ledger_id,
            and_(
                LedgerEntry.ledger_id.is_(None),
                LedgerEntry.user_open_id == legacy_subject,
            ),
        )

    def _budget_scope(self, legacy_subject: str) -> Any:
        return or_(
            CategoryBudget.ledger_id == self._request_context().ledger_id,
            and_(
                CategoryBudget.ledger_id.is_(None),
                CategoryBudget.user_open_id == legacy_subject,
            ),
        )

    @staticmethod
    def _entry_order_by() -> tuple[Any, ...]:
        """Stable newest-first order shared by last-entry and list pagination."""
        return (
            LedgerEntry.occurred_at.desc(),
            LedgerEntry.created_at.desc(),
            LedgerEntry.id.desc(),
        )

    def _latest_query(self, user_open_id: str) -> Select[tuple[LedgerEntry]]:
        return (
            select(LedgerEntry)
            .where(self._entry_scope(user_open_id), LedgerEntry.deleted_at.is_(None))
            .order_by(*self._entry_order_by())
            .limit(1)
        )

    @staticmethod
    def _keyset_before_cursor(cursor: LedgerEntry) -> Any:
        """Return SQL expression for rows strictly older than the cursor row."""
        return or_(
            LedgerEntry.occurred_at < cursor.occurred_at,
            and_(
                LedgerEntry.occurred_at == cursor.occurred_at,
                LedgerEntry.created_at < cursor.created_at,
            ),
            and_(
                LedgerEntry.occurred_at == cursor.occurred_at,
                LedgerEntry.created_at == cursor.created_at,
                LedgerEntry.id < cursor.id,
            ),
        )

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
