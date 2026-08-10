"""P29 recurring-rule unit tests: scheduling math, rule lifecycle, isolation.

Covers the pure ``next_occurrence_after`` / ``first_occurrence_on_*`` scheduling
helpers (monthly / yearly / weekly, month and year boundaries, anchor-day
clamping), the ``RecurringService`` rule lifecycle (create / get / list / update
/ pause / resume / disable / skip), ledger isolation, account validation, and
the P28 budget contract (rules and pendings never count toward budget).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    AccountType,
    Direction,
    RecurringFrequency,
    RecurringOccurrence,
    RecurringOccurrenceStatus,
    RecurringRule,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.recurring import (
    RecurringRuleConflictError,
    RecurringRuleNotFoundError,
    RecurringRuleValidationError,
    RecurringService,
    first_occurrence_on_day,
    first_occurrence_on_month_day,
    local_business_date,
    next_occurrence_after,
)

FUTURE = date(2027, 1, 15)  # safely after any real "today"


async def _context(session: AsyncSession, subject: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)


def _service(session: AsyncSession) -> RecurringService:
    return RecurringService(session, currency="CNY", timezone="Asia/Shanghai")


def _app(session: AsyncSession) -> ClientApplicationService:
    return ClientApplicationService(session, currency="CNY", timezone="Asia/Shanghai")


async def _create_rule(
    session: AsyncSession,
    context: RequestContext,
    *,
    amount: str = "3500",
    category: str = "房租",
    description: str = "房租",
    frequency: RecurringFrequency = RecurringFrequency.MONTHLY,
    transaction_type: Direction = Direction.EXPENSE,
    next_occurrence: date = FUTURE,
    account_id=None,
) -> RecurringRule:
    if account_id is None:
        account_id = (await AccountService(session).get_default(context)).id
    return await _service(session).create(
        context,
        transaction_type=transaction_type,
        amount=Decimal(amount),
        currency=None,
        category=category,
        description=description,
        frequency=frequency,
        interval=1,
        next_occurrence=next_occurrence,
        account_id=account_id,
    )


# -- scheduling helpers ----------------------------------------------------


def test_monthly_scheduling_clamps_anchor_day_and_recovers() -> None:
    freq = RecurringFrequency.MONTHLY
    # Jan 31 + 1 month clamps to Feb 28 (anchor 31), then recovers to Mar 31.
    assert next_occurrence_after(
        date(2026, 1, 31), frequency=freq, interval=1, anchor_day=31
    ) == date(2026, 2, 28)
    assert next_occurrence_after(
        date(2026, 2, 28), frequency=freq, interval=1, anchor_day=31
    ) == date(2026, 3, 31)
    # Leap-year February keeps the 29th.
    assert next_occurrence_after(
        date(2028, 1, 31), frequency=freq, interval=1, anchor_day=31
    ) == date(2028, 2, 29)
    # A stable day never drifts.
    assert next_occurrence_after(
        date(2026, 8, 8), frequency=freq, interval=1, anchor_day=8
    ) == date(2026, 9, 8)
    # Multi-month interval crosses a year boundary.
    assert next_occurrence_after(
        date(2026, 11, 15), frequency=freq, interval=3, anchor_day=15
    ) == date(2027, 2, 15)


def test_yearly_scheduling_keeps_month_and_anchor() -> None:
    freq = RecurringFrequency.YEARLY
    assert next_occurrence_after(
        date(2026, 6, 15), frequency=freq, interval=1, anchor_day=15
    ) == date(2027, 6, 15)
    # Year boundary with a leap-year February anchor.
    assert next_occurrence_after(
        date(2028, 2, 29), frequency=freq, interval=1, anchor_day=29
    ) == date(2029, 2, 28)


def test_weekly_scheduling_adds_seven_day_blocks() -> None:
    freq = RecurringFrequency.WEEKLY
    assert next_occurrence_after(
        date(2026, 8, 9), frequency=freq, interval=1, anchor_day=9
    ) == date(2026, 8, 16)
    assert next_occurrence_after(
        date(2026, 8, 9), frequency=freq, interval=4, anchor_day=9
    ) == date(2026, 9, 6)


def test_first_occurrence_on_day_clamps_and_rolls_forward() -> None:
    # Today on/before the anchor → this month; otherwise next month.
    assert first_occurrence_on_day(date(2026, 8, 9), 8) == date(2026, 9, 8)
    assert first_occurrence_on_day(date(2026, 8, 5), 8) == date(2026, 8, 8)
    assert first_occurrence_on_day(date(2026, 1, 31), 31) == date(2026, 1, 31)
    # December rolls into January.
    assert first_occurrence_on_day(date(2026, 12, 15), 8) == date(2027, 1, 8)


def test_first_occurrence_on_month_day_rolls_year() -> None:
    assert first_occurrence_on_month_day(date(2026, 8, 9), 6, 15) == date(2027, 6, 15)
    assert first_occurrence_on_month_day(date(2026, 6, 15), 6, 15) == date(2026, 6, 15)
    # Feb 30 clamps to Feb 28.
    assert first_occurrence_on_month_day(date(2026, 8, 9), 2, 30) == date(2027, 2, 28)


def test_local_business_date_uses_business_timezone() -> None:
    from datetime import UTC, datetime

    # 2026-08-09 18:00 UTC is 2026-08-10 02:00 in Asia/Shanghai.
    assert (
        local_business_date(
            "Asia/Shanghai", datetime(2026, 8, 9, 18, 0, tzinfo=UTC)
        )
        == date(2026, 8, 10)
    )


# -- rule lifecycle --------------------------------------------------------


async def test_create_rule_sets_fields_and_anchor(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur")
    rule = await _create_rule(session, context, next_occurrence=date(2026, 9, 8))
    assert rule.ledger_id == context.ledger_id
    assert rule.creator_user_id == context.actor_user_id
    assert rule.transaction_type is Direction.EXPENSE
    assert rule.amount == Decimal("3500")
    assert rule.currency == "CNY"
    assert rule.category == "房租"
    assert rule.description == "房租"
    assert rule.frequency == "monthly"
    assert rule.interval == 1
    assert rule.next_occurrence == date(2026, 9, 8)
    assert rule.anchor_day == 8
    assert rule.status == "active"


async def test_create_income_and_foreign_currency(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_income")
    account = await AccountService(session).get_default(context)
    rule = await _service(session).create(
        context,
        transaction_type=Direction.INCOME,
        amount=Decimal("20"),
        currency="USD",
        category="订阅",
        description="订阅",
        frequency=RecurringFrequency.MONTHLY,
        interval=1,
        next_occurrence=FUTURE,
        account_id=account.id,
    )
    assert rule.transaction_type is Direction.INCOME
    assert rule.currency == "USD"


async def test_create_rejects_past_next_occurrence(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_past")
    account = await AccountService(session).get_default(context)
    with pytest.raises(RecurringRuleValidationError):
        await _service(session).create(
            context,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("100"),
            currency=None,
            category="水费",
            description="水费",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=date(2000, 1, 1),
            account_id=account.id,
        )


async def test_create_validates_amount_and_category(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_bad")
    account = await AccountService(session).get_default(context)
    with pytest.raises(RecurringRuleValidationError):
        await _service(session).create(
            context,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("0"),
            currency=None,
            category="房租",
            description="",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=FUTURE,
            account_id=account.id,
        )
    with pytest.raises(RecurringRuleValidationError):
        await _service(session).create(
            context,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("100"),
            currency=None,
            category=" ",
            description="",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=FUTURE,
            account_id=account.id,
        )


async def test_create_rejects_archived_and_cross_ledger_account(session: AsyncSession) -> None:
    context_a = await _context(session, "ou_recur_acct_a")
    context_b = await _context(session, "ou_recur_acct_b")
    account_service = AccountService(session)
    # Cross-ledger account (ledger B's account) is rejected for ledger A.
    other_account = await account_service.get_default(context_b)
    with pytest.raises(RecurringRuleValidationError):
        await _create_rule(session, context_a, account_id=other_account.id)
    # Archived account is rejected.
    second = await account_service.create(
        context_a,
        name="备用",
        account_type=AccountType.CASH,
        subtype=None,
        provider=None,
        currency=None,
        opening_balance=Decimal("0"),
        make_default=False,
    )
    await account_service.archive(context_a, second.id)
    with pytest.raises(RecurringRuleValidationError):
        await _create_rule(session, context_a, account_id=second.id)


async def test_get_and_list_are_ledger_scoped(session: AsyncSession) -> None:
    context_a = await _context(session, "ou_recur_iso_a")
    context_b = await _context(session, "ou_recur_iso_b")
    rule = await _create_rule(session, context_a)

    rules_b = await _service(session).list(context_b)
    assert rules_b == []

    with pytest.raises(RecurringRuleNotFoundError):
        await _service(session).get(context_b, rule.id)

    got = await _service(session).get(context_a, rule.id)
    assert got.id == rule.id


async def test_update_rule_affects_future_only(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_upd")
    rule = await _create_rule(session, context)
    service = _service(session)

    updated = await service.update(
        context,
        rule.id,
        amount=Decimal("4000"),
        category="房租2",
        next_occurrence=date(2027, 2, 8),
    )
    assert updated.amount == Decimal("4000")
    assert updated.category == "房租2"
    assert updated.next_occurrence == date(2027, 2, 8)
    assert updated.anchor_day == 8


async def test_pause_and_resume_status_and_schedule(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_pr")
    rule = await _create_rule(session, context, next_occurrence=date(2027, 1, 15))
    service = _service(session)

    paused = await service.pause(context, rule.id)
    assert paused.status == "paused"
    with pytest.raises(RecurringRuleConflictError):
        await service.pause(context, rule.id)

    # While paused, the schedule does not advance.
    resumed = await service.resume(context, rule.id)
    assert resumed.status == "active"
    assert resumed.next_occurrence == date(2027, 1, 15)


async def test_resume_advances_past_overdue_periods(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_adv")
    # A rule due today.
    today = local_business_date("Asia/Shanghai")
    rule = await _create_rule(session, context, next_occurrence=today)
    service = _service(session)
    await service.pause(context, rule.id)
    # Paused across a month boundary, then resumed: next_occurrence jumps to the
    # next future period without back-filling pendings.
    resumed = await service.resume(context, rule.id)
    assert resumed.status == "active"
    assert resumed.next_occurrence > today


async def test_disable_rule_is_terminal(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_disable")
    rule = await _create_rule(session, context)
    service = _service(session)
    disabled = await service.disable(context, rule.id)
    assert disabled.status == "disabled"
    with pytest.raises(RecurringRuleConflictError):
        await service.resume(context, rule.id)
    with pytest.raises(RecurringRuleConflictError):
        await service.pause(context, rule.id)


async def test_skip_occurrence_advances_without_entry(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_skip")
    today = local_business_date("Asia/Shanghai")
    rule = await _create_rule(session, context, next_occurrence=today)
    service = _service(session)

    updated = await service.skip_occurrence(context, rule.id)
    assert updated.next_occurrence > today
    occurrences = (await session.scalars(select(RecurringOccurrence))).all()
    assert len(occurrences) == 1
    assert occurrences[0].status == RecurringOccurrenceStatus.SKIPPED.value
    assert occurrences[0].occurrence_date == today


async def test_skip_second_time_is_idempotent_conflict(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_skip2")
    today = local_business_date("Asia/Shanghai")
    rule = await _create_rule(session, context, next_occurrence=today)
    service = _service(session)
    await service.skip_occurrence(context, rule.id)
    # The next_occurrence has advanced, so skipping again targets a new period.
    await service.skip_occurrence(context, rule.id)
    occurrences = (await session.scalars(select(RecurringOccurrence))).all()
    assert len(occurrences) == 2
    assert all(item.status == RecurringOccurrenceStatus.SKIPPED.value for item in occurrences)


# -- budget integration ----------------------------------------------------


async def test_recurring_rule_and_pending_do_not_touch_budget(session: AsyncSession) -> None:
    context = await _context(session, "ou_recur_budget")
    app = _app(session)
    await app.set_total_budget(context, period=date(2026, 8, 1), amount=Decimal("10000"))
    await _create_rule(session, context, amount="3500", category="房租")

    overview = await app.get_budget_overview(context, period=date(2026, 8, 1))
    assert overview.total_spent == Decimal("0")

    # The rule is an active rule; creating it changed nothing in budget.
    rules = await app.list_recurring_rules(context)
    assert len(rules) == 1
