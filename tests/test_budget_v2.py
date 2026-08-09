"""P28 Budget 2.0 unit tests: period-scoped budgets, ledger isolation, statistics.

Covers the ``BudgetService`` progress query and write commands plus the unified
``ClientApplicationService`` budget boundary: total / category CRUD, unique
constraints, ledger and period isolation, expense-only statistics semantics
(income / transfer / delete / restore / revision), no-budget categories, the
recurring ``CategoryBudget`` fallback, household reuse, and permission checks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    AccountType,
    Budget,
    CategoryBudget,
    Direction,
    LedgerEntry,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.ledger_authorization import LedgerAuthorizationError
from lark_ledger.services.transfers import TransferService

AUG = date(2026, 8, 1)


async def _context(session: AsyncSession, subject: str = "ou_budget") -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)


def _app(session: AsyncSession) -> ClientApplicationService:
    return ClientApplicationService(session, currency="CNY", timezone="Asia/Shanghai")


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


async def _create(
    session: AsyncSession,
    context: RequestContext,
    amount: str,
    category: str,
    *,
    direction: Direction = Direction.EXPENSE,
    occurred_at: datetime | None = None,
    note: str = "",
) -> LedgerEntry:
    service = LedgerService(session, currency="CNY", timezone="Asia/Shanghai")
    await service.execute(
        context,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal(amount),
            direction=direction,
            category=category,
            occurred_at=occurred_at or _at(15),
            note=note,
        ),
        source_type="test",
    )
    entry = (
        await session.scalars(
            select(LedgerEntry)
            .where(
                LedgerEntry.ledger_id == context.ledger_id,
                LedgerEntry.category == category,
            )
            .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        )
    ).first()
    assert entry is not None
    return entry


async def _update(
    session: AsyncSession, context: RequestContext, entry: LedgerEntry, **fields: object
) -> None:
    service = LedgerService(session, currency="CNY", timezone="Asia/Shanghai")
    await service.execute(
        context,
        ParsedCommand(
            action=Action.UPDATE_ENTRY,
            entry_ref=str(entry.short_id),
            amount=fields.get("amount"),
            category=fields.get("category"),
            direction=fields.get("direction"),
        ),
        source_type="test",
    )


async def _delete(
    session: AsyncSession, context: RequestContext, entry: LedgerEntry
) -> None:
    service = LedgerService(session, currency="CNY", timezone="Asia/Shanghai")
    await service.execute(
        context,
        ParsedCommand(action=Action.DELETE_ENTRY, entry_ref=str(entry.short_id)),
        source_type="test",
    )


async def _restore(
    session: AsyncSession, context: RequestContext, entry: LedgerEntry
) -> None:
    service = LedgerService(session, currency="CNY", timezone="Asia/Shanghai")
    await service.execute(
        context,
        ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref=str(entry.short_id)),
        source_type="test",
    )


# -- CRUD and unique constraints ------------------------------------------


async def test_total_and_category_budget_crud(session: AsyncSession) -> None:
    context = await _context(session)
    app = _app(session)

    overview = await app.set_total_budget(context, period=AUG, amount=Decimal("12000"))
    assert overview.total_limit_set is True
    assert overview.total_budget == Decimal("12000")
    assert overview.status == "normal"  # limit exists; zero usage so far

    rows = (await session.scalars(select(Budget))).all()
    assert len(rows) == 1
    assert rows[0].category is None and rows[0].amount == Decimal("12000")

    overview = await app.set_total_budget(context, period=AUG, amount=Decimal("9000"))
    assert overview.total_budget == Decimal("9000")
    assert len((await session.scalars(select(Budget))).all()) == 1

    overview = await app.set_category_budget(
        context, period=AUG, category="餐饮", amount=Decimal("3000")
    )
    assert overview.total_limit_set is True
    food = next(item for item in overview.items if item.category == "餐饮")
    assert food.amount == Decimal("3000")
    assert food.status == "normal"

    overview = await app.set_category_budget(
        context, period=AUG, category="餐饮", amount=Decimal("4000")
    )
    food = next(item for item in overview.items if item.category == "餐饮")
    assert food.amount == Decimal("4000")

    overview = await app.delete_budget(context, period=AUG, category="餐饮")
    assert [item for item in overview.items if item.category == "餐饮"] == []
    assert overview.allocated == Decimal("0")
    assert overview.unallocated == Decimal("9000")

    overview = await app.delete_budget(context, period=AUG, category=None)
    assert overview.total_limit_set is False
    assert overview.total_budget is None
    assert overview.status == "none"
    assert (await session.scalars(select(Budget))).all() == []


async def test_budget_amount_and_category_validation(session: AsyncSession) -> None:
    context = await _context(session)
    app = _app(session)
    with pytest.raises(ValueError, match="at least 0.01"):
        await app.set_total_budget(context, period=AUG, amount=Decimal("0"))
    with pytest.raises(ValueError, match="required"):
        await app.set_category_budget(
            context, period=AUG, category="  ", amount=Decimal("100")
        )
    with pytest.raises(ValueError, match="at most 64"):
        await app.set_category_budget(
            context, period=AUG, category="餐" * 65, amount=Decimal("100")
        )


async def test_unique_constraints_total_and_category(session: AsyncSession) -> None:
    context = await _context(session)
    session.add(
        Budget(ledger_id=context.ledger_id, period=AUG, category=None, amount=Decimal("100"))
    )
    await session.flush()
    session.add(
        Budget(ledger_id=context.ledger_id, period=AUG, category=None, amount=Decimal("200"))
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    session.add(
        Budget(ledger_id=context.ledger_id, period=AUG, category="餐饮", amount=Decimal("100"))
    )
    await session.flush()
    session.add(
        Budget(ledger_id=context.ledger_id, period=AUG, category="餐饮", amount=Decimal("200"))
    )
    with pytest.raises(IntegrityError):
        await session.flush()


# -- ledger and period isolation ------------------------------------------


async def test_ledger_isolation(session: AsyncSession) -> None:
    context_a = await _context(session, "ou_a")
    context_b = await _context(session, "ou_b")
    app = _app(session)

    await app.set_total_budget(context_a, period=AUG, amount=Decimal("12000"))
    await app.set_category_budget(context_a, period=AUG, category="餐饮", amount=Decimal("3000"))

    overview_b = await app.get_budget_overview(context_b, period=AUG)
    assert overview_b.total_budget is None
    assert overview_b.items == []

    # Deleting a category budget in ledger B must not touch ledger A's rows.
    await app.delete_budget(context_b, period=AUG, category="餐饮")
    rows = (
        await session.scalars(
            select(Budget).where(Budget.ledger_id == context_a.ledger_id)
        )
    ).all()
    assert len(rows) == 2


async def test_period_isolation(session: AsyncSession) -> None:
    context = await _context(session)
    app = _app(session)
    july = date(2026, 7, 1)
    september = date(2026, 9, 1)

    await app.set_total_budget(context, period=AUG, amount=Decimal("12000"))
    await app.set_category_budget(context, period=AUG, category="餐饮", amount=Decimal("3000"))
    await app.set_category_budget(context, period=july, category="餐饮", amount=Decimal("2000"))
    await app.set_category_budget(context, period=september, category="交通", amount=Decimal("500"))

    await _create(session, context, "100", "餐饮", occurred_at=datetime(2026, 7, 20, tzinfo=UTC))
    await _create(session, context, "150", "餐饮", occurred_at=datetime(2026, 8, 20, tzinfo=UTC))
    await _create(session, context, "40", "交通", occurred_at=datetime(2026, 8, 21, tzinfo=UTC))
    await _create(session, context, "999", "餐饮", occurred_at=datetime(2026, 9, 5, tzinfo=UTC))

    august = await app.get_budget_overview(context, period=AUG)
    assert august.total_spent == Decimal("190")
    assert august.total_budget == Decimal("12000")
    food = next(item for item in august.items if item.category == "餐饮")
    assert food.amount == Decimal("3000")
    assert food.spent == Decimal("150")
    transport = next(item for item in august.items if item.category == "交通")
    assert transport.amount is None  # no budget in August for 交通
    assert transport.spent == Decimal("40")
    assert transport.status == "none"

    july_view = await app.get_budget_overview(context, period=july)
    assert july_view.total_spent == Decimal("100")
    assert july_view.total_limit_set is False  # no explicit total limit in July
    assert july_view.total_budget == Decimal("2000")  # derived from the category limit

    september_view = await app.get_budget_overview(context, period=september)
    assert september_view.total_spent == Decimal("999")
    september_food = next(item for item in september_view.items if item.category == "餐饮")
    assert september_food.amount is None and september_food.spent == Decimal("999")
    september_transport = next(
        item for item in september_view.items if item.category == "交通"
    )
    assert september_transport.amount == Decimal("500")


async def test_month_boundaries_use_ledger_timezone(session: AsyncSession) -> None:
    context = await _context(session)
    app = _app(session)
    await app.set_total_budget(context, period=AUG, amount=Decimal("12000"))

    # 2026-07-31 15:59 UTC == 2026-07-31 23:59 +08:00 -> July.
    await _create(
        session, context, "50", "餐饮", occurred_at=datetime(2026, 7, 31, 15, 59, tzinfo=UTC)
    )
    # 2026-07-31 16:00 UTC == 2026-08-01 00:00 +08:00 -> August.
    await _create(
        session, context, "60", "餐饮", occurred_at=datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
    )

    august = await app.get_budget_overview(context, period=AUG)
    assert august.total_spent == Decimal("60")
    july = await app.get_budget_overview(context, period=date(2026, 7, 1))
    assert july.total_spent == Decimal("50")


# -- statistics semantics -------------------------------------------------


async def test_expense_only_and_transfer_excluded(session: AsyncSession) -> None:
    context = await _context(session)
    accounts = AccountService(session)
    wallet = await accounts.create(
        context, name="支付宝", account_type=AccountType.ASSET, opening_balance=Decimal("0")
    )
    app = _app(session)
    await app.set_total_budget(context, period=AUG, amount=Decimal("1000"))

    await _create(session, context, "100", "餐饮")
    await _create(
        session,
        context,
        "500",
        "工资",
        direction=Direction.INCOME,
    )
    await TransferService(session).create(
        context,
        from_account_id=(await accounts.get_default(context)).id,
        to_account_id=wallet.id,
        amount=Decimal("120"),
        occurred_at=_at(15),
    )

    overview = await app.get_budget_overview(context, period=AUG)
    assert overview.total_spent == Decimal("100")  # income and transfer excluded
    food = next(item for item in overview.items if item.category == "餐饮")
    assert food.spent == Decimal("100")


async def test_delete_restore_and_revision_recompute_actuals(session: AsyncSession) -> None:
    context = await _context(session)
    app = _app(session)
    await app.set_total_budget(context, period=AUG, amount=Decimal("1000"))

    food = await _create(session, context, "100", "餐饮")
    assert (await app.get_budget_overview(context, period=AUG)).total_spent == Decimal("100")

    await _delete(session, context, food)
    assert (await app.get_budget_overview(context, period=AUG)).total_spent == Decimal("0")

    await _restore(session, context, food)
    assert (await app.get_budget_overview(context, period=AUG)).total_spent == Decimal("100")

    await _update(session, context, food, amount=Decimal("150"))
    overview = await app.get_budget_overview(context, period=AUG)
    assert overview.total_spent == Decimal("150")
    assert next(i for i in overview.items if i.category == "餐饮").spent == Decimal("150")

    await _update(session, context, food, category="交通")
    overview = await app.get_budget_overview(context, period=AUG)
    # 餐饮 now has neither spend nor a budget, so it no longer appears.
    assert all(i.category != "餐饮" for i in overview.items)
    transport_item = next(i for i in overview.items if i.category == "交通")
    assert transport_item.spent == Decimal("150")
    assert transport_item.amount is None


async def test_no_budget_category_reports_actual_with_none_status(session: AsyncSession) -> None:
    context = await _context(session)
    await _create(session, context, "120", "餐饮")
    await _create(session, context, "80", "交通")
    app = _app(session)

    overview = await app.get_budget_overview(context, period=AUG)
    assert overview.total_budget is None
    assert overview.total_limit_set is False
    assert overview.total_spent == Decimal("200")
    items = {item.category: item for item in overview.items}
    assert items["餐饮"].amount is None
    assert items["餐饮"].spent == Decimal("120")
    assert items["餐饮"].status == "none"
    assert items["餐饮"].usage_rate is None
    assert items["交通"].remaining is None


async def test_status_thresholds_normal_warning_exceeded(session: AsyncSession) -> None:
    context = await _context(session)
    app = _app(session)
    await app.set_category_budget(context, period=AUG, category="餐饮", amount=Decimal("1000"))

    await _create(session, context, "700", "餐饮")
    assert (
        next(i for i in (await app.get_budget_overview(context, period=AUG)).items
             if i.category == "餐饮").status
        == "normal"
    )
    await _create(session, context, "100", "餐饮")  # 80%
    assert (
        next(i for i in (await app.get_budget_overview(context, period=AUG)).items
             if i.category == "餐饮").status
        == "warning"
    )
    await _create(session, context, "250", "餐饮")  # 105%
    item = next(
        i for i in (await app.get_budget_overview(context, period=AUG)).items
        if i.category == "餐饮"
    )
    assert item.status == "exceeded"
    assert item.remaining == Decimal("-50")
    assert item.usage_rate == Decimal("105.00")


async def test_recurring_budget_fallback_and_period_override(session: AsyncSession) -> None:
    context = await _context(session)
    session.add(
        CategoryBudget(
            user_open_id="ou_budget",
            ledger_id=context.ledger_id,
            category="餐饮",
            amount=Decimal("500"),
        )
    )
    await session.commit()
    app = _app(session)
    await _create(session, context, "100", "餐饮")

    overview = await app.get_budget_overview(context, period=AUG)
    food = next(i for i in overview.items if i.category == "餐饮")
    assert food.amount == Decimal("500")  # recurring fallback
    assert food.status == "normal"

    await app.set_category_budget(context, period=AUG, category="餐饮", amount=Decimal("100"))
    overview = await app.get_budget_overview(context, period=AUG)
    food = next(i for i in overview.items if i.category == "餐饮")
    assert food.amount == Decimal("100")  # period row wins
    assert food.status == "warning"  # 100 spent against a 100 limit


# -- household reuse and permissions --------------------------------------


async def test_feishu_set_total_budget_and_list_budgets(session: AsyncSession) -> None:
    context = await _context(session)
    service = LedgerService(
        session,
        currency="CNY",
        timezone="Asia/Shanghai",
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
    )

    result = await service.execute(
        context, ParsedCommand(action=Action.SET_TOTAL_BUDGET, amount=Decimal("12000"))
    )
    assert "已设置2026年8月总预算 ¥12000.00" in result.message
    assert "本月已用 ¥0.00" in result.message
    assert "剩余 ¥12000.00" in result.message
    assert result.budget_alert is None

    await _create(session, context, "500", "餐饮")
    listed = await service.execute(context, ParsedCommand(action=Action.LIST_BUDGETS))
    assert "总预算 ¥12000.00" in listed.message
    assert "已用 ¥500.00" in listed.message
    assert "餐饮：未设置预算，本月已用 ¥500.00" in listed.message

    filtered = await service.execute(
        context, ParsedCommand(action=Action.LIST_BUDGETS, category="交通")
    )
    assert "还没有设置交通月预算。" in filtered.message

    # Over-spend reports the exceeded remaining amount.
    over = await service.execute(
        context, ParsedCommand(action=Action.SET_TOTAL_BUDGET, amount=Decimal("100"))
    )
    assert "已超出 ¥400.00" in over.message


async def test_household_ledger_reuses_budget_domain(session: AsyncSession) -> None:
    owner = await _context(session, "ou_owner")
    app = _app(session)
    household = await app.create_household(owner, "家")
    household_context = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=household.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_owner",
    )

    overview = await app.set_total_budget(household_context, period=AUG, amount=Decimal("8000"))
    assert overview.total_budget == Decimal("8000")
    assert overview.total_limit_set is True

    # The personal ledger keeps no budget.
    personal = await app.get_budget_overview(owner, period=AUG)
    assert personal.total_budget is None
    assert personal.items == []


async def test_permission_denied_for_unrelated_ledger(session: AsyncSession) -> None:
    context_a = await _context(session, "ou_a")
    context_b = await _context(session, "ou_b")
    forged = RequestContext(
        actor_user_id=context_a.actor_user_id,
        ledger_id=context_b.ledger_id,
        source_channel="feishu",
        external_subject_id="ou_a",
    )
    with pytest.raises(LedgerAuthorizationError):
        await _app(session).set_total_budget(forged, period=AUG, amount=Decimal("100"))
    with pytest.raises(LedgerAuthorizationError):
        await _app(session).set_category_budget(
            forged, period=AUG, category="餐饮", amount=Decimal("100")
        )
    with pytest.raises(LedgerAuthorizationError):
        await _app(session).delete_budget(forged, period=AUG, category="餐饮")
