"""Internal entry-mutation mixin for ``LedgerService`` (split from ``ledger.py``).

Module-private and shared names that used to live in the ledger facade live
here so the public ``lark_ledger.services.ledger`` module can keep its import
surface unchanged.
"""
# mypy: disable-error-code="attr-defined"

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.entry_revisions import RevisionChangeType, snapshot_ledger_entry
from lark_ledger.models import Direction, LedgerEntry, LedgerEntryRevision
from lark_ledger.schemas import (
    DEFAULT_LIST_LIMIT,
    MAX_BATCH_BUDGETS,
    MAX_BATCH_ENTRIES,
    MAX_LIST_LIMIT,
    SUPPORTED_INPUT_CURRENCIES,
    Action,
    EntryCandidate,
    ExecutionResult,
    ParsedCommand,
)
from lark_ledger.services.exchange import ExchangeRateUnavailableError
from lark_ledger.short_id import (
    MAX_SHORT_ID_ALLOCATION_ATTEMPTS,
    ShortIdError,
    format_entry_ref,
    normalize_entry_ref,
)

_NOT_FOUND_MSG = "未找到该账目，或该账目不属于当前用户。"

logger = logging.getLogger("lark_ledger.services.ledger")


class EntryConflictError(RuntimeError):
    """The entry changed after a Web client loaded it."""


@dataclass(frozen=True)
class _ConvertedAmount:
    amount: Decimal
    original_amount: Decimal
    original_currency: str

    @property
    def was_converted(self) -> bool:
        return self.original_currency != ""


class _EntryMixin:
    session: AsyncSession
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
            ledger_id=self._request_context().ledger_id,
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
        """Allocate a ledger-scoped short_id.

        Avoids collisions with rows already in the database and with other
        pending ``LedgerEntry`` objects in this session (important for batches).
        The database unique constraint ``uq_entries_ledger_short_id`` remains the
        authoritative guarantee under concurrency.
        """
        for _ in range(MAX_SHORT_ID_ALLOCATION_ATTEMPTS):
            candidate = self._short_id_factory()
            if await self._short_id_is_taken(user_open_id, candidate):
                continue
            return candidate  # type: ignore[no-any-return]
        raise RuntimeError(
            f"failed to allocate short_id after {MAX_SHORT_ID_ALLOCATION_ATTEMPTS} attempts"
        )

    async def _short_id_is_taken(self, user_open_id: str, short_id: str) -> bool:
        for obj in self.session.new:
            if (
                isinstance(obj, LedgerEntry)
                and obj.ledger_id == self._request_context().ledger_id
                and obj.short_id == short_id
            ):
                return True
        existing = await self.session.scalar(
            select(LedgerEntry.id)
            .where(
                self._entry_scope(user_open_id),
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
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        expected_updated_at: datetime | None,
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
        self._assert_expected_updated_at(entry, expected_updated_at)
        if entry.deleted_at is not None:
            return ExecutionResult(
                message=(
                    f"{format_entry_ref(entry.short_id)} 已删除，请先恢复后再修改。"
                )
            )
        return await self._apply_entry_update(user_open_id, entry, command)

    async def _delete_entry(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        expected_updated_at: datetime | None,
    ) -> ExecutionResult:
        assert command.entry_ref is not None
        try:
            short_id = normalize_entry_ref(command.entry_ref)
        except ShortIdError:
            return ExecutionResult(message="短 ID 格式无效。请使用五位编号，例如：删除 #A83F2")
        entry = await self._find_entry_for_mutation(user_open_id, short_id)
        if entry is None:
            return ExecutionResult(message=_NOT_FOUND_MSG)
        self._assert_expected_updated_at(entry, expected_updated_at)
        return await self._apply_entry_delete(user_open_id, entry, last_style=False)

    async def _restore_entry(
        self,
        user_open_id: str,
        command: ParsedCommand,
        *,
        expected_updated_at: datetime | None,
    ) -> ExecutionResult:
        assert command.entry_ref is not None
        try:
            short_id = normalize_entry_ref(command.entry_ref)
        except ShortIdError:
            return ExecutionResult(message="短 ID 格式无效。请使用五位编号，例如：恢复 #A83F2")
        entry = await self._find_entry_for_mutation(user_open_id, short_id)
        if entry is None:
            return ExecutionResult(message=_NOT_FOUND_MSG)
        self._assert_expected_updated_at(entry, expected_updated_at)
        return await self._apply_entry_restore(user_open_id, entry)

    @staticmethod
    def _assert_expected_updated_at(
        entry: LedgerEntry, expected_updated_at: datetime | None
    ) -> None:
        if expected_updated_at is None:
            return
        actual = entry.updated_at
        if actual.tzinfo is None:
            actual = actual.replace(tzinfo=UTC)
        if expected_updated_at.tzinfo is None:
            expected_updated_at = expected_updated_at.replace(tzinfo=UTC)
        if actual != expected_updated_at:
            raise EntryConflictError("entry was modified by another request")

    async def _find_latest_for_mutation(self, user_open_id: str) -> LedgerEntry | None:
        query = self._latest_query(user_open_id)
        if self._supports_for_update():
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def _find_entry_for_mutation(
        self, user_open_id: str, short_id: str
    ) -> LedgerEntry | None:
        query = select(LedgerEntry).where(
            self._entry_scope(user_open_id),
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

        entry.updated_at = datetime.now(UTC)
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
        entry.updated_at = datetime.now(UTC)
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
        entry.updated_at = datetime.now(UTC)
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
                ledger_id=self._request_context().ledger_id,
                actor_user_id=self._request_context().actor_user_id,
                short_id=entry.short_id,
                change_type=change_type.value,
                before_json=before,
                after_json=after,
            )
        )

    async def _list_entries(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        requested = command.limit if command.limit is not None else DEFAULT_LIST_LIMIT
        capped = requested > MAX_LIST_LIMIT
        limit = min(max(requested, 1), MAX_LIST_LIMIT)
        filters: list[Any] = [
            self._entry_scope(user_open_id),
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
            self._entry_scope(user_open_id),
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
