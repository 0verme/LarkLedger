"""Recurring-rule domain service (P29).

A ``RecurringRule`` is a known future recurring income / expense for a ledger.
Rules never write ledger transactions directly — the Recurring Worker turns a
due rule into a deterministic confirmation pending (frozen account / amount /
category / planned date) plus a proactive Feishu reminder, and only a confirmed
pending becomes a real ``LedgerEntry``. This module owns the rule lifecycle
(create / list / get / update / pause / resume / disable / skip) and the pure
scheduling math; the worker and pending-confirmation hooks live next to their
own stores so they share the same domain vocabulary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Direction,
    PendingCommand,
    PendingStatus,
    RecurringFrequency,
    RecurringOccurrence,
    RecurringOccurrenceStatus,
    RecurringRule,
    RecurringRuleStatus,
)
from lark_ledger.services.accounts import AccountError, AccountService
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.member_resolution import MemberResolutionService

MAX_MONEY = Decimal("999999999999.99")
MAX_CATEGORY_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 200
MIN_INTERVAL = 1
MAX_INTERVAL = 100
DEFAULT_INTERVAL = 1


class RecurringRuleError(ValueError):
    """Base error for the recurring-rule domain."""


class RecurringRuleNotFoundError(RecurringRuleError):
    pass


class RecurringRuleConflictError(RecurringRuleError):
    pass


class RecurringRuleValidationError(RecurringRuleError):
    pass


def days_in_month(year: int, month: int) -> int:
    """Return the number of days in ``year`` / ``month`` (calendar-based)."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


def next_occurrence_after(
    current: date,
    *,
    frequency: RecurringFrequency,
    interval: int,
    anchor_day: int,
) -> date:
    """Return the next occurrence strictly after ``current``.

    The schedule is business-semantic and deterministic:

    * ``weekly`` — exactly ``7 * interval`` days later.
    * ``monthly`` — ``interval`` months later, with the day clamped to
      ``anchor_day`` (a 31st rule schedules Feb 28 / 29, then back to the 31st).
    * ``yearly`` — ``interval`` years later, keeping the month of ``current``
      and clamping the day to ``anchor_day``.

    ``anchor_day`` is the stable day-of-month the rule was created / last reset
    with, so month-boundary clamping never drifts the schedule.
    """
    if frequency is RecurringFrequency.WEEKLY:
        from datetime import timedelta

        return current + timedelta(days=7 * interval)
    if frequency is RecurringFrequency.MONTHLY:
        total_months = current.year * 12 + (current.month - 1) + interval
        year = total_months // 12
        month = total_months % 12 + 1
        day = min(anchor_day, days_in_month(year, month))
        return date(year, month, day)
    year = current.year + interval
    day = min(anchor_day, days_in_month(year, current.month))
    return date(year, current.month, day)


def first_occurrence_on_day(today: date, day: int) -> date:
    """Return the first occurrence on ``day`` of a month on or after ``today``.

    When ``today`` is on or before ``day`` the result is this month's ``day``;
    otherwise the next month's ``day``. The day is clamped for short months.
    """
    target = min(day, days_in_month(today.year, today.month))
    candidate = date(today.year, today.month, target)
    if candidate < today:
        next_month = 1 if today.month == 12 else today.month + 1
        next_year = today.year + 1 if today.month == 12 else today.year
        candidate = date(
            next_year, next_month, min(day, days_in_month(next_year, next_month))
        )
    return candidate


def first_occurrence_on_month_day(today: date, month: int, day: int) -> date:
    """Return the first annual occurrence on ``month`` / ``day`` at/after today."""
    target = min(day, days_in_month(today.year, month))
    candidate = date(today.year, month, target)
    if candidate < today:
        candidate = date(
            today.year + 1, month, min(day, days_in_month(today.year + 1, month))
        )
    return candidate


def local_business_date(timezone: str, now: datetime | None = None) -> date:
    """Return the business date in ``timezone`` for ``now`` (UTC default)."""
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(timezone)).date()


class RecurringService:
    """Rule lifecycle commands, all ledger-scoped and authorization-checked."""

    def __init__(self, session: AsyncSession, *, currency: str, timezone: str) -> None:
        self._session = session
        self._currency = currency
        self._timezone = ZoneInfo(timezone)
        self._authorization = LedgerAuthorizationService(session)

    # -- commands ---------------------------------------------------------

    async def create(
        self,
        context: RequestContext,
        *,
        transaction_type: Direction,
        amount: Decimal,
        currency: str | None,
        category: str,
        description: str,
        frequency: RecurringFrequency,
        interval: int,
        next_occurrence: date,
        account_id: uuid.UUID,
        paid_by_user_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> RecurringRule:
        ledger = await self._authorization.get_accessible(
            context.actor_user_id, context.ledger_id
        )
        amount = self._validate_amount(amount)
        name = self._validate_category(category)
        detail = self._validate_description(description)
        frequency = self._validate_frequency(frequency)
        interval = self._validate_interval(interval)
        currency_code = self._resolve_currency(currency or ledger.currency)
        await self._validate_account(context, account_id)
        payer = await self._validate_payer(context, paid_by_user_id)
        today = local_business_date(str(self._timezone), now)
        if next_occurrence < today:
            raise RecurringRuleValidationError("下次发生日期不能早于今天")
        rule = RecurringRule(
            ledger_id=context.ledger_id,
            creator_user_id=context.actor_user_id,
            paid_by_user_id=payer,
            account_id=account_id,
            transaction_type=transaction_type,
            amount=amount,
            currency=currency_code,
            category=name,
            description=detail,
            frequency=frequency.value,
            interval=interval,
            next_occurrence=next_occurrence,
            anchor_day=next_occurrence.day,
            status=RecurringRuleStatus.ACTIVE.value,
        )
        self._session.add(rule)
        await self._session.flush()
        return rule

    async def get(self, context: RequestContext, rule_id: uuid.UUID) -> RecurringRule:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        row = await self._session.scalar(
            select(RecurringRule).where(
                RecurringRule.id == rule_id,
                RecurringRule.ledger_id == context.ledger_id,
            )
        )
        if row is None:
            raise RecurringRuleNotFoundError("周期账单不存在或不属于当前账本")
        return row

    async def list(self, context: RequestContext) -> list[RecurringRule]:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        return list(
            (
                await self._session.scalars(
                    select(RecurringRule)
                    .where(RecurringRule.ledger_id == context.ledger_id)
                    .order_by(RecurringRule.next_occurrence, RecurringRule.created_at)
                )
            ).all()
        )

    async def update(
        self,
        context: RequestContext,
        rule_id: uuid.UUID,
        *,
        transaction_type: Direction | None = None,
        amount: Decimal | None = None,
        currency: str | None = None,
        category: str | None = None,
        description: str | None = None,
        frequency: RecurringFrequency | None = None,
        interval: int | None = None,
        next_occurrence: date | None = None,
        account_id: uuid.UUID | None = None,
        paid_by_user_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> RecurringRule:
        rule = await self._locked(context, rule_id)
        if account_id is not None and account_id != rule.account_id:
            await self._validate_account(context, account_id)
            rule.account_id = account_id
        if paid_by_user_id is not None and paid_by_user_id != rule.paid_by_user_id:
            rule.paid_by_user_id = await self._validate_payer(context, paid_by_user_id)
        if amount is not None:
            rule.amount = self._validate_amount(amount)
        if currency is not None:
            rule.currency = self._resolve_currency(currency)
        if category is not None:
            rule.category = self._validate_category(category)
        if description is not None:
            rule.description = self._validate_description(description)
        if transaction_type is not None:
            rule.transaction_type = transaction_type
        if frequency is not None:
            rule.frequency = self._validate_frequency(frequency).value
        if interval is not None:
            rule.interval = self._validate_interval(interval)
        if next_occurrence is not None:
            today = local_business_date(str(self._timezone), now)
            if next_occurrence < today:
                raise RecurringRuleValidationError("下次发生日期不能早于今天")
            rule.next_occurrence = next_occurrence
            rule.anchor_day = next_occurrence.day
        await self._session.flush()
        return rule

    async def pause(self, context: RequestContext, rule_id: uuid.UUID) -> RecurringRule:
        rule = await self._locked(context, rule_id)
        if rule.status == RecurringRuleStatus.DISABLED.value:
            raise RecurringRuleConflictError("已停用的周期账单不能暂停")
        if rule.status == RecurringRuleStatus.PAUSED.value:
            raise RecurringRuleConflictError("该周期账单已处于暂停状态")
        rule.status = RecurringRuleStatus.PAUSED.value
        await self._session.flush()
        return rule

    async def resume(self, context: RequestContext, rule_id: uuid.UUID) -> RecurringRule:
        rule = await self._locked(context, rule_id)
        if rule.status == RecurringRuleStatus.DISABLED.value:
            raise RecurringRuleConflictError("已停用的周期账单不能恢复")
        if rule.status == RecurringRuleStatus.ACTIVE.value:
            raise RecurringRuleConflictError("该周期账单已处于启用状态")
        # Resume skips straight to the next valid future period; it never
        # back-fills pendings for the paused window.
        today = local_business_date(str(self._timezone))
        while rule.next_occurrence <= today:
            rule.next_occurrence = next_occurrence_after(
                rule.next_occurrence,
                frequency=RecurringFrequency(rule.frequency),
                interval=rule.interval,
                anchor_day=rule.anchor_day,
            )
        rule.status = RecurringRuleStatus.ACTIVE.value
        await self._session.flush()
        return rule

    async def disable(self, context: RequestContext, rule_id: uuid.UUID) -> RecurringRule:
        rule = await self._locked(context, rule_id)
        if rule.status == RecurringRuleStatus.DISABLED.value:
            raise RecurringRuleConflictError("该周期账单已停用")
        rule.status = RecurringRuleStatus.DISABLED.value
        await self._session.flush()
        return rule

    async def skip_occurrence(
        self,
        context: RequestContext,
        rule_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> RecurringRule:
        """Skip the current scheduled occurrence and advance the schedule.

        Records a ``skipped`` occurrence (the unique ``(rule_id, occurrence_date)``
        constraint makes this idempotent), cancels a still-pending confirmation
        if one exists, and advances ``next_occurrence``. No transaction is
        created and later periods keep working.
        """
        rule = await self._locked(context, rule_id)
        if rule.status != RecurringRuleStatus.ACTIVE.value:
            raise RecurringRuleConflictError("只有启用中的周期账单可以跳过本期")
        current = now or datetime.now(UTC)
        occurrence_date = rule.next_occurrence
        occurrence = await self._session.scalar(
            select(RecurringOccurrence).where(
                RecurringOccurrence.rule_id == rule.id,
                RecurringOccurrence.occurrence_date == occurrence_date,
            )
        )
        if occurrence is not None:
            if occurrence.status == RecurringOccurrenceStatus.CONFIRMED.value:
                raise RecurringRuleConflictError("该期已确认入账，无法跳过")
            if occurrence.status in {
                RecurringOccurrenceStatus.CANCELLED.value,
                RecurringOccurrenceStatus.FAILED.value,
            }:
                raise RecurringRuleConflictError("该期已处理，无法跳过")
            if occurrence.status == RecurringOccurrenceStatus.PENDING.value:
                if occurrence.pending_id is not None:
                    await self._session.execute(
                        update(PendingCommand)
                        .where(
                            PendingCommand.id == occurrence.pending_id,
                            PendingCommand.status == PendingStatus.PENDING.value,
                        )
                        .values(
                            status=PendingStatus.CANCELLED.value,
                            cancelled_at=current,
                        )
                    )
                occurrence.status = RecurringOccurrenceStatus.SKIPPED.value
        else:
            self._session.add(
                RecurringOccurrence(
                    ledger_id=rule.ledger_id,
                    rule_id=rule.id,
                    occurrence_date=occurrence_date,
                    status=RecurringOccurrenceStatus.SKIPPED.value,
                )
            )
        rule.next_occurrence = next_occurrence_after(
            occurrence_date,
            frequency=RecurringFrequency(rule.frequency),
            interval=rule.interval,
            anchor_day=rule.anchor_day,
        )
        await self._session.flush()
        return rule

    # -- helpers ----------------------------------------------------------

    async def _locked(self, context: RequestContext, rule_id: uuid.UUID) -> RecurringRule:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        row = await self._session.scalar(
            select(RecurringRule)
            .where(
                RecurringRule.id == rule_id,
                RecurringRule.ledger_id == context.ledger_id,
            )
            .with_for_update()
        )
        if row is None:
            raise RecurringRuleNotFoundError("周期账单不存在或不属于当前账本")
        return row

    async def _validate_account(self, context: RequestContext, account_id: uuid.UUID) -> None:
        try:
            await AccountService(self._session).get(context, account_id, require_active=True)
        except AccountError as exc:
            raise RecurringRuleValidationError(
                "账户不存在、已归档或不属于当前账本"
            ) from exc

    async def _validate_payer(
        self, context: RequestContext, paid_by_user_id: uuid.UUID | None
    ) -> uuid.UUID:
        """Validate a payer is a ledger member; defaults to the acting user."""
        payer = paid_by_user_id or context.actor_user_id
        if not await MemberResolutionService(self._session).is_member(context, payer):
            raise RecurringRuleValidationError("付款人不存在或不属于当前账本")
        return payer

    @staticmethod
    def _validate_amount(amount: Decimal) -> Decimal:
        if amount <= 0:
            raise RecurringRuleValidationError("周期账单金额必须大于 0")
        if amount > MAX_MONEY:
            raise RecurringRuleValidationError("周期账单金额超出支持范围")
        return amount

    @staticmethod
    def _validate_category(category: str) -> str:
        name = (category or "").strip()
        if not name:
            raise RecurringRuleValidationError("分类不能为空")
        if len(name) > MAX_CATEGORY_LENGTH:
            raise RecurringRuleValidationError("分类名称过长")
        return name

    @staticmethod
    def _validate_description(description: str) -> str:
        detail = (description or "").strip()
        if len(detail) > MAX_DESCRIPTION_LENGTH:
            raise RecurringRuleValidationError("描述过长")
        return detail

    @staticmethod
    def _validate_frequency(frequency: RecurringFrequency) -> RecurringFrequency:
        try:
            return RecurringFrequency(str(frequency).lower())
        except ValueError as exc:
            raise RecurringRuleValidationError("不支持的周期频率") from exc

    @staticmethod
    def _validate_interval(interval: int) -> int:
        if not isinstance(interval, int) or interval < MIN_INTERVAL or interval > MAX_INTERVAL:
            raise RecurringRuleValidationError(
                f"间隔必须是 {MIN_INTERVAL} 到 {MAX_INTERVAL} 之间的整数"
            )
        return interval

    def _resolve_currency(self, currency: str) -> str:
        code = (currency or self._currency).strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise RecurringRuleValidationError("币种必须是三位字母代码")
        return code
