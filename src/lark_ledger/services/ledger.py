import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.entry_revisions import (
    RevisionChangeType,
    snapshot_ledger_entry,
)
from lark_ledger.models import (
    BudgetAlert,
    CategoryBudget,
    Direction,
    LedgerEntry,
    LedgerEntryRevision,
)
from lark_ledger.schemas import (
    DEFAULT_EXPORT_DAYS,
    DEFAULT_LIST_LIMIT,
    MAX_BATCH_BUDGETS,
    MAX_BATCH_ENTRIES,
    MAX_EXPORT_ROWS,
    MAX_LIST_LIMIT,
    SUPPORTED_INPUT_CURRENCIES,
    Action,
    BudgetCandidate,
    CategoryTotal,
    EntryCandidate,
    ExecutionResult,
    ParsedCommand,
    ReportData,
    TrendPoint,
)
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError
from lark_ledger.services.export import ExportTooLargeError, build_export_file
from lark_ledger.short_id import (
    MAX_SHORT_ID_ALLOCATION_ATTEMPTS,
    ShortIdError,
    format_entry_ref,
    generate_short_id,
    normalize_entry_ref,
)

_NOT_FOUND_MSG = "未找到该账目，或该账目不属于当前用户。"

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
    "我可以帮你记账、修改、撤销、查看账单、导出 CSV、汇总、设置预算和生成消费报告。试试：\n"
    "• 午饭32\n• 昨天打车38.5\n• 工资到账10000\n"
    "• 早餐12，午饭32，打车45，餐饮预算1000\n"
    "• 最近10笔\n• 查看 #A83F2\n• 把 #A83F2 改成35元\n"
    "• 删除 #A83F2\n• 恢复 #A83F2\n• 上一笔改成8块\n"
    "• 导出本月账单\n• 导出最近90天账单\n• 导出全部账单\n"
    "• 这个月餐饮花了多少\n• 撤销刚才那笔\n"
    "• 交通预算500，人情往来预算1000\n• 查看预算\n• 生成这个月的消费图表"
)
NOTE_PREVIEW_LEN = 20


class LedgerService:
    def __init__(
        self,
        session: AsyncSession,
        currency: str = "CNY",
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
        exchange_rates: ExchangeRateService | None = None,
        short_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.session = session
        self.currency = currency
        self.timezone = ZoneInfo(timezone)
        self.now = now
        self.exchange_rates = exchange_rates
        self._short_id_factory = short_id_factory or generate_short_id

    async def execute(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str = "text",
        source_message_id: str | None = None,
        source_item_index: int = 0,
    ) -> ExecutionResult:
        if command.action is Action.CREATE:
            return await self._create(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
                source_item_index=source_item_index,
            )
        if command.action is Action.BATCH:
            return await self._execute_batch(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
            )
        if command.action is Action.CREATE_ENTRIES:
            return await self._create_entries(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
            )
        if command.action is Action.UPDATE_LAST:
            return await self._update_last(user_open_id, command)
        if command.action is Action.UNDO_LAST:
            return await self._undo_last(user_open_id)
        if command.action is Action.LIST_ENTRIES:
            return await self._list_entries(user_open_id, command)
        if command.action is Action.GET_ENTRY:
            return await self._get_entry(user_open_id, command)
        if command.action is Action.UPDATE_ENTRY:
            return await self._update_entry(user_open_id, command)
        if command.action is Action.DELETE_ENTRY:
            return await self._delete_entry(user_open_id, command)
        if command.action is Action.RESTORE_ENTRY:
            return await self._restore_entry(user_open_id, command)
        if command.action is Action.EXPORT_ENTRIES:
            return await self._export_entries(user_open_id, command)
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
            .where(LedgerEntry.user_open_id == user_open_id, LedgerEntry.deleted_at.is_(None))
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

    async def _create(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None,
        source_item_index: int,
    ) -> ExecutionResult:
        entry, converted, budget_alert = await self._stage_entry(
            user_open_id,
            command,
            source_type=source_type,
            source_message_id=source_message_id,
            source_item_index=source_item_index,
        )
        await self.session.commit()
        return ExecutionResult(
            message=self._created_message(entry, converted),
            budget_alert=budget_alert,
        )

    async def _stage_entry(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None,
        source_item_index: int,
    ) -> tuple[LedgerEntry, _ConvertedAmount, str | None]:
        assert command.amount is not None
        assert command.direction is not None
        assert command.category is not None
        assert command.occurred_at is not None
        converted = await self._convert_command_amount(command)
        entry = LedgerEntry(
            user_open_id=user_open_id,
            short_id=await self._allocate_short_id(user_open_id),
            amount=converted.amount,
            currency=self.currency,
            direction=command.direction,
            category=command.category,
            note=command.note or "",
            occurred_at=command.occurred_at,
            source_type=source_type,
            source_message_id=source_message_id,
            source_item_index=source_item_index if source_message_id is not None else None,
        )
        self.session.add(entry)
        budget_alert = await self._check_budget(entry)
        await self.session.flush()
        return entry, converted, budget_alert

    async def _allocate_short_id(self, user_open_id: str) -> str:
        """Allocate a user-scoped short_id.

        Avoids collisions with rows already in the database and with other
        pending ``LedgerEntry`` objects in this session (important for batches).
        The database unique constraint ``uq_entries_user_short_id`` remains the
        authoritative guarantee under concurrency.
        """
        for _ in range(MAX_SHORT_ID_ALLOCATION_ATTEMPTS):
            candidate = self._short_id_factory()
            if await self._short_id_is_taken(user_open_id, candidate):
                continue
            return candidate
        raise RuntimeError(
            f"failed to allocate short_id after {MAX_SHORT_ID_ALLOCATION_ATTEMPTS} attempts"
        )

    async def _short_id_is_taken(self, user_open_id: str, short_id: str) -> bool:
        for obj in self.session.new:
            if (
                isinstance(obj, LedgerEntry)
                and obj.user_open_id == user_open_id
                and obj.short_id == short_id
            ):
                return True
        existing = await self.session.scalar(
            select(LedgerEntry.id)
            .where(
                LedgerEntry.user_open_id == user_open_id,
                LedgerEntry.short_id == short_id,
            )
            .limit(1)
        )
        return existing is not None

    def _created_message(self, entry: LedgerEntry, converted: _ConvertedAmount) -> str:
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        note = f"（{entry.note}）" if entry.note else ""
        conversion = self._conversion_note(converted)
        return (
            f"已记录 {format_entry_ref(entry.short_id)} {sign} "
            f"{self._format_money(entry.amount)}{conversion} · {entry.category}{note}"
        )

    async def _create_entries(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None,
    ) -> ExecutionResult:
        assert command.entries is not None
        successes: list[str] = []
        failures: list[str] = []
        alerts: list[str] = []
        income_total = Decimal("0")
        expense_total = Decimal("0")

        for index, candidate in enumerate(command.entries):
            item, error = self._validated_entry_candidate(candidate)
            label = f"第 {index + 1} 笔"
            if item is None:
                failures.append(f"❌ {label}：{error}")
                continue
            try:
                async with self.session.begin_nested():
                    entry, converted, budget_alert = await self._stage_entry(
                        user_open_id,
                        item,
                        source_type=source_type,
                        source_message_id=source_message_id,
                        source_item_index=index,
                    )
            except ExchangeRateUnavailableError:
                failures.append(f"❌ {label}：暂时无法获取汇率")
            except ValueError:
                failures.append(f"❌ {label}：换算后的金额超出支持范围")
            except Exception:
                logger.exception("failed to persist batch ledger item %s", index + 1)
                failures.append(f"❌ {label}：保存失败，请稍后重试")
            else:
                if entry.direction is Direction.INCOME:
                    income_total += entry.amount
                else:
                    expense_total += entry.amount
                successes.append(self._batch_entry_line(index, entry, converted))
                if budget_alert and budget_alert not in alerts:
                    alerts.append(budget_alert)

        await self.session.commit()
        lines = [
            f"批量图片记账完成：成功 {len(successes)} 笔，失败 {len(failures)} 笔",
            f"收入合计 {self._format_money(income_total)} · "
            f"支出合计 {self._format_money(expense_total)}",
            *successes,
            *failures,
        ]
        if command.batch_truncated:
            lines.append(
                f"⚠️ 图片中的流水超过 {MAX_BATCH_ENTRIES} 笔，"
                f"本次仅处理前 {MAX_BATCH_ENTRIES} 笔。"
            )
        return ExecutionResult(
            message="\n".join(lines),
            budget_alert="\n\n".join(alerts) or None,
        )

    async def _execute_batch(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None,
    ) -> ExecutionResult:
        entry_successes: list[str] = []
        entry_failures: list[str] = []
        budget_successes: list[str] = []
        budget_failures: list[str] = []
        alerts: list[str] = []
        income_total = Decimal("0")
        expense_total = Decimal("0")

        for index, entry_candidate in enumerate(command.entries or []):
            item, error = self._validated_entry_candidate(entry_candidate)
            label = f"第 {index + 1} 笔"
            if item is None:
                entry_failures.append(f"❌ {label}：{error}")
                continue
            try:
                async with self.session.begin_nested():
                    entry, converted, budget_alert = await self._stage_entry(
                        user_open_id,
                        item,
                        source_type=source_type,
                        source_message_id=source_message_id,
                        source_item_index=index,
                    )
            except ExchangeRateUnavailableError:
                entry_failures.append(f"❌ {label}：暂时无法获取汇率")
            except ValueError:
                entry_failures.append(f"❌ {label}：换算后的金额超出支持范围")
            except Exception:
                logger.exception("failed to persist text batch ledger item %s", index + 1)
                entry_failures.append(f"❌ {label}：保存失败，请稍后重试")
            else:
                if entry.direction is Direction.INCOME:
                    income_total += entry.amount
                else:
                    expense_total += entry.amount
                entry_successes.append(self._batch_entry_line(index, entry, converted))
                if budget_alert and budget_alert not in alerts:
                    alerts.append(budget_alert)

        budgets = command.budgets or []
        last_indexes: dict[str, int] = {}
        for index, budget_candidate in enumerate(budgets):
            category = (budget_candidate.category or "").strip()
            if category:
                last_indexes[category] = index

        for index, budget_candidate in enumerate(budgets):
            category = (budget_candidate.category or "").strip()
            if category and last_indexes[category] != index:
                continue
            item, error = self._validated_budget_candidate(budget_candidate)
            label = category or f"第 {index + 1} 项"
            if item is None:
                budget_failures.append(f"❌ {label}：{error}")
                continue
            try:
                async with self.session.begin_nested():
                    converted, spent = await self._stage_budget(user_open_id, item)
            except ExchangeRateUnavailableError:
                budget_failures.append(f"❌ {label}：暂时无法获取汇率")
            except ValueError:
                budget_failures.append(f"❌ {label}：换算后的金额超出支持范围")
            except Exception:
                logger.exception("failed to persist text batch budget item %s", index + 1)
                budget_failures.append(f"❌ {label}：保存失败，请稍后重试")
            else:
                budget_successes.append(
                    "✅ " + self._budget_set_message(item, converted, spent).replace("\n", "；")
                )

        await self.session.commit()
        lines = [
            "复杂指令处理完成："
            f"账目成功 {len(entry_successes)} 笔、失败 {len(entry_failures)} 笔；"
            f"预算成功 {len(budget_successes)} 项、失败 {len(budget_failures)} 项",
            f"收入合计 {self._format_money(income_total)} · "
            f"支出合计 {self._format_money(expense_total)}",
            *entry_successes,
            *entry_failures,
            *budget_successes,
            *budget_failures,
        ]
        if command.batch_truncated:
            lines.append(
                f"⚠️ 消息中的账目超过 {MAX_BATCH_ENTRIES} 笔，"
                f"本次仅处理前 {MAX_BATCH_ENTRIES} 笔。"
            )
        if command.budgets_truncated:
            lines.append(
                f"⚠️ 消息中的预算超过 {MAX_BATCH_BUDGETS} 项，"
                f"本次仅处理前 {MAX_BATCH_BUDGETS} 项。"
            )
        return ExecutionResult(
            message="\n".join(lines),
            budget_alert="\n\n".join(alerts) or None,
        )

    def _batch_entry_line(
        self, index: int, entry: LedgerEntry, converted: _ConvertedAmount
    ) -> str:
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        occurred = self._local_datetime(entry.occurred_at).strftime("%m-%d %H:%M")
        note = entry.note.strip()
        if len(note) > 30:
            note = note[:29] + "…"
        suffix = f" · {note}" if note else ""
        return (
            f"✅ {format_entry_ref(entry.short_id)} 第 {index + 1} 笔："
            f"{sign} {self._format_money(entry.amount)}"
            f"{self._conversion_note(converted)} · {entry.category} · {occurred}{suffix}"
        )

    @staticmethod
    def _validated_entry_candidate(
        candidate: EntryCandidate,
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
        if candidate.direction is None or not str(candidate.direction).strip():
            return None, "缺少收支方向"
        if candidate.occurred_at is None or (
            isinstance(candidate.occurred_at, str) and not candidate.occurred_at.strip()
        ):
            return None, "缺少发生时间"
        currency = (candidate.currency or "").strip().upper() or None
        if currency is not None and currency not in SUPPORTED_INPUT_CURRENCIES:
            return None, f"不支持币种 {currency[:12]}"
        try:
            direction = Direction(str(candidate.direction).lower())
        except ValueError:
            return None, "收支方向无效"
        try:
            item = ParsedCommand(
                action=Action.CREATE,
                amount=candidate.amount,
                currency=currency,
                direction=direction,
                category=category,
                note=(candidate.note or "").strip() or None,
                occurred_at=candidate.occurred_at,
            )
        except ValidationError:
            return None, "字段格式无效或超出支持范围"
        return item, None

    async def _update_last(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        entry = await self._find_latest_for_mutation(user_open_id)
        if entry is None:
            return ExecutionResult(message="还没有可以修改的记录。")
        return await self._apply_entry_update(user_open_id, entry, command)

    async def _undo_last(self, user_open_id: str) -> ExecutionResult:
        entry = await self._find_latest_for_mutation(user_open_id)
        if entry is None:
            return ExecutionResult(message="还没有可以撤销的记录。")
        return await self._apply_entry_delete(user_open_id, entry, last_style=True)

    async def _update_entry(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.entry_ref is not None
        try:
            short_id = normalize_entry_ref(command.entry_ref)
        except ShortIdError:
            return ExecutionResult(
                message="短 ID 格式无效。请使用五位编号，例如：把 #A83F2 改成35元"
            )
        entry = await self._find_entry_for_mutation(user_open_id, short_id)
        if entry is None:
            return ExecutionResult(message=_NOT_FOUND_MSG)
        if entry.deleted_at is not None:
            return ExecutionResult(
                message=(
                    f"{format_entry_ref(entry.short_id)} 已删除，请先恢复后再修改。"
                )
            )
        return await self._apply_entry_update(user_open_id, entry, command)

    async def _delete_entry(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.entry_ref is not None
        try:
            short_id = normalize_entry_ref(command.entry_ref)
        except ShortIdError:
            return ExecutionResult(message="短 ID 格式无效。请使用五位编号，例如：删除 #A83F2")
        entry = await self._find_entry_for_mutation(user_open_id, short_id)
        if entry is None:
            return ExecutionResult(message=_NOT_FOUND_MSG)
        return await self._apply_entry_delete(user_open_id, entry, last_style=False)

    async def _restore_entry(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.entry_ref is not None
        try:
            short_id = normalize_entry_ref(command.entry_ref)
        except ShortIdError:
            return ExecutionResult(message="短 ID 格式无效。请使用五位编号，例如：恢复 #A83F2")
        entry = await self._find_entry_for_mutation(user_open_id, short_id)
        if entry is None:
            return ExecutionResult(message=_NOT_FOUND_MSG)
        return await self._apply_entry_restore(user_open_id, entry)

    async def _find_latest_for_mutation(self, user_open_id: str) -> LedgerEntry | None:
        query = self._latest_query(user_open_id)
        if self._supports_for_update():
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def _find_entry_for_mutation(
        self, user_open_id: str, short_id: str
    ) -> LedgerEntry | None:
        query = select(LedgerEntry).where(
            LedgerEntry.user_open_id == user_open_id,
            LedgerEntry.short_id == short_id,
        )
        if self._supports_for_update():
            query = query.with_for_update()
        return (await self.session.execute(query.limit(1))).scalar_one_or_none()

    def _supports_for_update(self) -> bool:
        bind = self.session.get_bind()
        return bind is not None and bind.dialect.name == "postgresql"

    async def _apply_entry_update(
        self,
        user_open_id: str,
        entry: LedgerEntry,
        command: ParsedCommand,
    ) -> ExecutionResult:
        before = snapshot_ledger_entry(entry)
        converted = (
            await self._convert_command_amount(command) if command.amount is not None else None
        )
        changed = False
        if converted is not None and entry.amount != converted.amount:
            entry.amount = converted.amount
            changed = True
        if command.direction is not None and entry.direction != command.direction:
            entry.direction = command.direction
            changed = True
        if command.category is not None and entry.category != command.category:
            entry.category = command.category
            changed = True
        if command.clear_note:
            if entry.note != "":
                entry.note = ""
                changed = True
        elif command.note is not None and entry.note != command.note:
            entry.note = command.note
            changed = True
        if command.occurred_at is not None and entry.occurred_at != command.occurred_at:
            entry.occurred_at = command.occurred_at
            changed = True
        if not changed:
            return ExecutionResult(
                message=f"{format_entry_ref(entry.short_id)} 没有变化，无需修改。"
            )

        budget_alert = await self._check_budget(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        after = snapshot_ledger_entry(entry)
        self._add_revision(
            user_open_id=user_open_id,
            entry=entry,
            change_type=RevisionChangeType.UPDATE,
            before=before,
            after=after,
        )
        await self.session.commit()
        conversion = self._conversion_note(converted) if converted is not None else ""
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        note = f" · {entry.note}" if entry.note else ""
        return ExecutionResult(
            message=(
                f"已修改 {format_entry_ref(entry.short_id)}：\n"
                f"{sign} {self._format_money(entry.amount)}{conversion} · "
                f"{entry.category}{note}"
            ),
            budget_alert=budget_alert,
        )

    async def _apply_entry_delete(
        self,
        user_open_id: str,
        entry: LedgerEntry,
        *,
        last_style: bool,
    ) -> ExecutionResult:
        if entry.deleted_at is not None:
            if last_style:
                return ExecutionResult(
                    message=f"{format_entry_ref(entry.short_id)} 已经处于删除状态。"
                )
            return ExecutionResult(
                message=f"{format_entry_ref(entry.short_id)} 已经处于删除状态。"
            )
        before = snapshot_ledger_entry(entry)
        entry.deleted_at = datetime.now(UTC)
        await self.session.flush()
        await self.session.refresh(entry)
        after = snapshot_ledger_entry(entry)
        self._add_revision(
            user_open_id=user_open_id,
            entry=entry,
            change_type=RevisionChangeType.DELETE,
            before=before,
            after=after,
        )
        await self.session.commit()
        if last_style:
            return ExecutionResult(
                message=(
                    f"已撤销 {format_entry_ref(entry.short_id)}："
                    f"{self._format_money(entry.amount)} · {entry.category}"
                )
            )
        return ExecutionResult(
            message=(
                f"已删除 {format_entry_ref(entry.short_id)}。\n"
                f"如需找回，请发送：恢复 {format_entry_ref(entry.short_id)}"
            )
        )

    async def _apply_entry_restore(
        self, user_open_id: str, entry: LedgerEntry
    ) -> ExecutionResult:
        if entry.deleted_at is None:
            return ExecutionResult(
                message=f"{format_entry_ref(entry.short_id)} 当前未被删除，无需恢复。"
            )
        before = snapshot_ledger_entry(entry)
        entry.deleted_at = None
        await self.session.flush()
        await self.session.refresh(entry)
        after = snapshot_ledger_entry(entry)
        self._add_revision(
            user_open_id=user_open_id,
            entry=entry,
            change_type=RevisionChangeType.RESTORE,
            before=before,
            after=after,
        )
        await self.session.commit()
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        note = f" · {entry.note}" if entry.note else ""
        return ExecutionResult(
            message=(
                f"已恢复 {format_entry_ref(entry.short_id)}：\n"
                f"{sign} {self._format_money(entry.amount)} · {entry.category}{note}"
            )
        )

    def _add_revision(
        self,
        *,
        user_open_id: str,
        entry: LedgerEntry,
        change_type: RevisionChangeType,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self.session.add(
            LedgerEntryRevision(
                entry_id=entry.id,
                user_open_id=user_open_id,
                short_id=entry.short_id,
                change_type=change_type.value,
                before_json=before,
                after_json=after,
            )
        )

    async def _set_budget(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        converted, spent = await self._stage_budget(user_open_id, command)
        await self.session.commit()
        return ExecutionResult(message=self._budget_set_message(command, converted, spent))

    async def _stage_budget(
        self, user_open_id: str, command: ParsedCommand
    ) -> tuple[_ConvertedAmount, Decimal]:
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
        return converted, spent

    def _budget_set_message(
        self,
        command: ParsedCommand,
        converted: _ConvertedAmount,
        spent: Decimal,
    ) -> str:
        assert command.category is not None
        progress = self._budget_progress(spent, converted.amount)
        conversion = self._conversion_note(converted)
        return (
            f"已设置每月{command.category}预算 "
            f"{self._format_money(converted.amount)}{conversion}\n"
            f"本月已用 ¥{spent:.2f} · {progress}"
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
        if command.budgets_truncated:
            lines.append(
                f"⚠️ 消息中的预算超过 {MAX_BATCH_BUDGETS} 项，"
                f"本次仅处理前 {MAX_BATCH_BUDGETS} 项。"
            )
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

    async def _list_entries(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        requested = command.limit if command.limit is not None else DEFAULT_LIST_LIMIT
        capped = requested > MAX_LIST_LIMIT
        limit = min(max(requested, 1), MAX_LIST_LIMIT)
        filters: list[Any] = [
            LedgerEntry.user_open_id == user_open_id,
            LedgerEntry.deleted_at.is_(None),
        ]
        if command.range_start is not None and command.range_end is not None:
            filters.append(LedgerEntry.occurred_at >= command.range_start)
            filters.append(LedgerEntry.occurred_at < command.range_end)
        if command.category:
            filters.append(LedgerEntry.category == command.category)
        if command.direction is not None:
            filters.append(LedgerEntry.direction == command.direction)

        if command.before_entry_ref:
            try:
                cursor_code = normalize_entry_ref(command.before_entry_ref)
            except ShortIdError:
                return ExecutionResult(
                    message=(
                        "分页短 ID 格式无效。请使用："
                        "查看 #A83F2 之前的10笔"
                    )
                )
            cursor = await self._entry_by_short_id(
                user_open_id, cursor_code, include_deleted=True
            )
            if cursor is None:
                return ExecutionResult(
                    message="未找到该账目，或该账目不属于当前用户。"
                )
            filters.append(self._keyset_before_cursor(cursor))

        # Fetch one extra row to detect whether another page exists.
        fetched = (
            (
                await self.session.execute(
                    select(LedgerEntry)
                    .where(*filters)
                    .order_by(*self._entry_order_by())
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        has_more = len(fetched) > limit
        rows = fetched[:limit]
        if not rows:
            return ExecutionResult(message="没有符合条件的账目。")

        lines = [
            self._entry_list_line(index, entry) for index, entry in enumerate(rows, start=1)
        ]
        header = f"最近 {len(rows)} 笔账目（不含已撤销）"
        parts: list[str] = [header, "", *lines]
        notes: list[str] = []
        if capped:
            notes.append(f"单次最多显示 {MAX_LIST_LIMIT} 笔，已按上限返回。")
        if has_more:
            last_ref = format_entry_ref(rows[-1].short_id)
            notes.append("继续查看更早记录：")
            notes.append(f"查看 {last_ref} 之前的{limit}笔")
        if notes:
            parts.extend(["", *notes])
        return ExecutionResult(message="\n".join(parts))

    async def _get_entry(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.entry_ref is not None
        try:
            short_id = normalize_entry_ref(command.entry_ref)
        except ShortIdError:
            return ExecutionResult(
                message="短 ID 格式无效。请使用五位编号，例如：查看 #A83F2"
            )
        entry = await self._entry_by_short_id(
            user_open_id, short_id, include_deleted=True
        )
        if entry is None:
            return ExecutionResult(message="未找到该账目，或该账目不属于当前用户。")
        return ExecutionResult(message=self._entry_detail_message(entry))

    async def _entry_by_short_id(
        self,
        user_open_id: str,
        short_id: str,
        *,
        include_deleted: bool,
    ) -> LedgerEntry | None:
        filters = [
            LedgerEntry.user_open_id == user_open_id,
            LedgerEntry.short_id == short_id,
        ]
        if not include_deleted:
            filters.append(LedgerEntry.deleted_at.is_(None))
        return (
            await self.session.execute(select(LedgerEntry).where(*filters).limit(1))
        ).scalar_one_or_none()

    def _entry_list_line(self, index: int, entry: LedgerEntry) -> str:
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        when = self._local_datetime(entry.occurred_at).strftime("%m-%d %H:%M")
        note = self._note_preview(entry.note)
        note_part = f" · {note}" if note else ""
        return (
            f"{index}. {format_entry_ref(entry.short_id)} · {when}\n"
            f"   {sign} {self._format_money(entry.amount)} · {entry.category}{note_part}"
        )

    def _entry_detail_message(self, entry: LedgerEntry) -> str:
        sign = "支出" if entry.direction is Direction.EXPENSE else "收入"
        when = self._local_datetime(entry.occurred_at).strftime("%Y-%m-%d %H:%M")
        created = self._local_datetime(entry.created_at).strftime("%Y-%m-%d %H:%M")
        updated = self._local_datetime(entry.updated_at).strftime("%Y-%m-%d %H:%M")
        note = entry.note.strip() or "（无）"
        if entry.deleted_at is not None:
            deleted = self._local_datetime(entry.deleted_at).strftime("%Y-%m-%d %H:%M")
            status_lines = ["状态：已删除", f"删除时间：{deleted}"]
        else:
            status_lines = ["状态：有效"]
        return "\n".join(
            [
                f"短 ID：{format_entry_ref(entry.short_id)}",
                *status_lines,
                f"发生时间：{when}",
                f"方向：{sign}",
                f"金额：{self._format_money(entry.amount)}",
                f"币种：{entry.currency}",
                f"分类：{entry.category}",
                f"备注：{note}",
                f"来源类型：{entry.source_type}",
                f"创建时间：{created}",
                f"更新时间：{updated}",
            ]
        )

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
        try:
            range_start, range_end, range_label = self._resolve_export_range(command)
        except ValueError:
            return ExecutionResult(
                message="导出时间范围无效：开始时间必须早于结束时间。"
            )

        filters: list[Any] = [LedgerEntry.user_open_id == user_open_id]
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
                    .limit(MAX_EXPORT_ROWS + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(fetched) > MAX_EXPORT_ROWS:
            return ExecutionResult(
                message=(
                    f"符合条件的账目超过 {MAX_EXPORT_ROWS} 笔，"
                    "请缩小导出时间范围后重试。"
                )
            )
        if not fetched:
            return ExecutionResult(message="该时间范围内没有可导出的账目。")

        try:
            export_file = build_export_file(
                fetched,
                timezone=self.timezone,
                when=self._current_local_datetime(),
                range_label=range_label,
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
