"""Internal budget mixin for ``LedgerService`` (split from ``ledger.py``)."""
# mypy: disable-error-code="attr-defined"

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import BudgetAlert, CategoryBudget, Direction, LedgerEntry
from lark_ledger.schemas import (
    MAX_BATCH_BUDGETS,
    SUPPORTED_INPUT_CURRENCIES,
    Action,
    BudgetCandidate,
    ExecutionResult,
    ParsedCommand,
)
from lark_ledger.services.budget import BudgetService
from lark_ledger.services.exchange import ExchangeRateUnavailableError
from lark_ledger.services.ledger_entries import _ConvertedAmount, logger
from lark_ledger.web_schemas import BudgetOverview


class _BudgetMixin:
    session: AsyncSession
    async def _set_budget(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        converted, spent = await self._stage_budget(user_open_id, command)
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
                ledger_id=self._request_context().ledger_id,
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

    async def _set_total_budget(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        assert command.amount is not None
        converted = await self._convert_command_amount(command)
        service = BudgetService(
            self.session, currency=self.currency, timezone=str(self.timezone)
        )
        overview = await service.set_total_budget(
            self._request_context(),
            amount=converted.amount,
            currency=self.currency,
            now=self.now,
        )
        return ExecutionResult(message=self._total_budget_set_message(overview, converted))

    def _total_budget_set_message(
        self, overview: BudgetOverview, converted: _ConvertedAmount
    ) -> str:
        assert overview.total_budget is not None
        period = f"{overview.period[:4]}年{int(overview.period[5:7])}月"
        conversion = self._conversion_note(converted)
        remaining = overview.total_remaining
        if remaining is None:
            remaining_text = "暂无预算数据"
        elif remaining < 0:
            remaining_text = f"已超出 {self._format_money(-remaining)}"
        else:
            remaining_text = f"剩余 {self._format_money(remaining)}"
        return (
            f"已设置{period}总预算 {self._format_money(overview.total_budget)}{conversion}\n"
            f"本月已用 {self._format_money(overview.total_spent)} · {remaining_text}"
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
                # A savepoint isolates a single budget item so one bad item does
                # not abort the rest; the outer transaction commits all successes
                # together (with the outbox in P06a) once, never per item.
                async with self.session.begin_nested():
                    result = await self._set_budget(user_open_id, item_command)
            except ExchangeRateUnavailableError:
                failures.append(f"❌ {label}：暂时无法获取汇率")
            except ValueError:
                failures.append(f"❌ {label}：换算后的金额超出支持范围")
            except Exception:
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

    async def _list_budgets(
        self, user_open_id: str, command: ParsedCommand
    ) -> ExecutionResult:
        service = BudgetService(
            self.session, currency=self.currency, timezone=str(self.timezone)
        )
        overview = await service.overview(self._request_context(), now=self.now)
        lines: list[str] = []
        # A category-filtered query reports only that category; the period total
        # line is reserved for the unfiltered overview.
        if not command.category and overview.total_budget is not None:
            remaining = overview.total_remaining
            if remaining is None:
                remaining_text = "暂无预算数据"
            elif remaining < 0:
                remaining_text = f"已超出 {self._format_money(-remaining)}"
            else:
                remaining_text = f"剩余 {self._format_money(remaining)}"
            lines.append(
                f"总预算 {self._format_money(overview.total_budget)} · "
                f"已用 {self._format_money(overview.total_spent)} · {remaining_text}"
            )
        for item in overview.items:
            if command.category and item.category != command.category:
                continue
            if item.amount is None:
                lines.append(
                    f"• {item.category}：未设置预算，本月已用 ¥{item.spent:.2f}"
                )
            else:
                lines.append(
                    f"• {item.category}：¥{item.spent:.2f} / ¥{item.amount:.2f}"
                    f"（{self._budget_progress(item.spent, item.amount)}）"
                )
        if not lines:
            if command.category:
                return ExecutionResult(message=f"还没有设置{command.category}月预算。")
            return ExecutionResult(message="还没有设置任何月预算。")
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
                    self._budget_scope(user_open_id),
                    CategoryBudget.category == category,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _monthly_spend(self, user_open_id: str, category: str) -> Decimal:
        start, end = self._current_month_bounds()
        amount = await self.session.scalar(
            select(func.sum(LedgerEntry.amount)).where(
                self._entry_scope(user_open_id),
                LedgerEntry.category == category,
                LedgerEntry.direction == Direction.EXPENSE,
                LedgerEntry.deleted_at.is_(None),
                LedgerEntry.occurred_at >= start,
                LedgerEntry.occurred_at < end,
            )
        )
        return Decimal(amount or 0)

    @classmethod
    def _budget_progress(cls, spent: Decimal, budget: Decimal) -> str:
        if spent > budget:
            return f"已超出 ¥{spent - budget:.2f}"
        return f"已用 {cls._usage_percent(spent, budget)}，剩余 ¥{budget - spent:.2f}"

    @staticmethod
    def _usage_percent(spent: Decimal, budget: Decimal) -> str:
        return f"{spent / budget * 100:.0f}%"
