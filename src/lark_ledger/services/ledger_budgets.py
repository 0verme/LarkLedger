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
from lark_ledger.services.exchange import ExchangeRateUnavailableError
from lark_ledger.services.ledger_entries import _ConvertedAmount, logger


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
        query = select(CategoryBudget).where(self._budget_scope(user_open_id))
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
