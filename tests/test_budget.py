from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import BudgetAlert, CategoryBudget, Direction
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ledger import LedgerService

NOW = datetime(2026, 8, 3, 4, tzinfo=UTC)
THIS_MONTH = datetime(2026, 8, 2, 4, tzinfo=UTC)


def budget_command(
    action: Action, category: str | None = None, amount: str | None = None
) -> ParsedCommand:
    return ParsedCommand(
        action=action,
        category=category,
        amount=Decimal(amount) if amount is not None else None,
    )


def expense(
    amount: str, *, category: str = "餐饮", occurred_at: datetime = THIS_MONTH
) -> ParsedCommand:
    return ParsedCommand(
        action=Action.CREATE,
        amount=Decimal(amount),
        direction=Direction.EXPENSE,
        category=category,
        occurred_at=occurred_at,
    )


async def test_budget_crud_progress_and_user_isolation(session: AsyncSession) -> None:
    service = LedgerService(session, now=NOW)
    result = await service.execute(
        "ou_a", budget_command(Action.SET_BUDGET, "餐饮", "100")
    )
    assert "每月餐饮预算 ¥100.00" in result.message
    assert "本月已用 ¥0.00" in result.message

    await service.execute("ou_a", expense("25"))
    listed = await service.execute("ou_a", budget_command(Action.LIST_BUDGETS))
    assert "餐饮：¥25.00 / ¥100.00" in listed.message
    assert "25%" in listed.message
    assert "餐饮" not in (
        await service.execute("ou_b", budget_command(Action.LIST_BUDGETS))
    ).message

    updated = await service.execute(
        "ou_a", budget_command(Action.SET_BUDGET, "餐饮", "200")
    )
    assert "¥200.00" in updated.message
    budgets = (await session.execute(select(CategoryBudget))).scalars().all()
    assert len(budgets) == 1

    deleted = await service.execute("ou_a", budget_command(Action.DELETE_BUDGET, "餐饮"))
    assert "已取消餐饮月预算" in deleted.message
    assert (await session.execute(select(CategoryBudget))).scalars().all() == []


async def test_budget_alerts_once_at_each_threshold(session: AsyncSession) -> None:
    service = LedgerService(session, now=NOW)
    await service.execute("ou_user", budget_command(Action.SET_BUDGET, "餐饮", "100"))

    assert (await service.execute("ou_user", expense("79"))).budget_alert is None
    warning = await service.execute("ou_user", expense("1"))
    assert warning.budget_alert is not None
    assert "剩余 ¥20.00（80%）" in warning.budget_alert
    assert (await service.execute("ou_user", expense("5"))).budget_alert is None

    exceeded = await service.execute("ou_user", expense("15"))
    assert exceeded.budget_alert is not None
    assert "已超额" in exceeded.budget_alert
    assert "超出 ¥0.00（100%）" in exceeded.budget_alert
    assert (await service.execute("ou_user", expense("1"))).budget_alert is None
    thresholds = (
        (await session.execute(select(BudgetAlert.threshold).order_by(BudgetAlert.threshold)))
        .scalars()
        .all()
    )
    assert thresholds == [80, 100]


async def test_direct_jump_only_reports_overage_and_marks_both_levels(
    session: AsyncSession,
) -> None:
    service = LedgerService(session, now=NOW)
    await service.execute("ou_user", budget_command(Action.SET_BUDGET, "交通", "100"))
    result = await service.execute("ou_user", expense("120", category="交通"))

    assert result.budget_alert is not None
    assert "已超额" in result.budget_alert
    assert "快用完" not in result.budget_alert
    thresholds = (await session.execute(select(BudgetAlert.threshold))).scalars().all()
    assert sorted(thresholds) == [80, 100]


async def test_update_can_trigger_but_income_and_previous_month_do_not(
    session: AsyncSession,
) -> None:
    service = LedgerService(session, now=NOW)
    await service.execute("ou_user", budget_command(Action.SET_BUDGET, "餐饮", "100"))
    await service.execute("ou_user", expense("50"))
    updated = await service.execute(
        "ou_user", ParsedCommand(action=Action.UPDATE_LAST, amount=Decimal("80"))
    )
    assert updated.budget_alert is not None
    assert "80%" in updated.budget_alert

    undone = await service.execute("ou_user", ParsedCommand(action=Action.UNDO_LAST))
    assert undone.budget_alert is None
    repeated = await service.execute("ou_user", expense("80"))
    assert repeated.budget_alert is None

    await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("100"),
            direction=Direction.INCOME,
            category="餐饮",
            occurred_at=THIS_MONTH,
        ),
    )
    previous = await service.execute(
        "ou_user", expense("100", occurred_at=datetime(2026, 7, 20, tzinfo=UTC))
    )
    assert previous.budget_alert is None


@pytest.mark.parametrize(
    ("user", "created", "updated"),
    [
        (
            "ou_category",
            expense("80", category="餐饮"),
            ParsedCommand(action=Action.UPDATE_LAST, category="交通"),
        ),
        (
            "ou_direction",
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("80"),
                direction=Direction.INCOME,
                category="交通",
                occurred_at=THIS_MONTH,
            ),
            ParsedCommand(action=Action.UPDATE_LAST, direction=Direction.EXPENSE),
        ),
        (
            "ou_date",
            expense("80", category="交通", occurred_at=datetime(2026, 7, 20, tzinfo=UTC)),
            ParsedCommand(action=Action.UPDATE_LAST, occurred_at=THIS_MONTH),
        ),
    ],
)
async def test_update_checks_resulting_category_direction_and_date(
    session: AsyncSession,
    user: str,
    created: ParsedCommand,
    updated: ParsedCommand,
) -> None:
    service = LedgerService(session, now=NOW)
    await service.execute(user, budget_command(Action.SET_BUDGET, "交通", "100"))
    assert (await service.execute(user, created)).budget_alert is None
    result = await service.execute(user, updated)
    assert result.budget_alert is not None
    assert "交通本月预算快用完了" in result.budget_alert


async def test_setting_budget_reports_progress_without_claiming_alert(
    session: AsyncSession,
) -> None:
    service = LedgerService(session, now=NOW)
    await service.execute("ou_user", expense("80"))
    configured = await service.execute(
        "ou_user", budget_command(Action.SET_BUDGET, "餐饮", "100")
    )
    assert configured.budget_alert is None
    assert "本月已用 ¥80.00" in configured.message
    assert (await session.execute(select(BudgetAlert))).scalars().all() == []

    next_expense = await service.execute("ou_user", expense("1"))
    assert next_expense.budget_alert is not None
    assert "81%" in next_expense.budget_alert


async def test_month_boundaries_use_configured_timezone(session: AsyncSession) -> None:
    now = datetime(2026, 7, 31, 16, 30, tzinfo=UTC)  # 2026-08-01 00:30 in Shanghai
    service = LedgerService(session, timezone="Asia/Shanghai", now=now)
    await service.execute("ou_user", budget_command(Action.SET_BUDGET, "餐饮", "100"))

    old_month = await service.execute(
        "ou_user", expense("80", occurred_at=datetime(2026, 7, 31, 15, 59, tzinfo=UTC))
    )
    assert old_month.budget_alert is None
    current_month = await service.execute(
        "ou_user", expense("80", occurred_at=datetime(2026, 7, 31, 16, tzinfo=UTC))
    )
    assert current_month.budget_alert is not None
    assert "80%" in current_month.budget_alert


async def test_alert_uniqueness_guards_against_duplicate_delivery(session: AsyncSession) -> None:
    budget = CategoryBudget(user_open_id="ou_user", category="餐饮", amount=Decimal("100"))
    session.add(budget)
    await session.flush()
    session.add(
        BudgetAlert(
            budget_id=budget.id,
            period_start=datetime(2026, 8, 1).date(),
            threshold=80,
        )
    )
    await session.commit()
    session.add(
        BudgetAlert(
            budget_id=budget.id,
            period_start=datetime(2026, 8, 1).date(),
            threshold=80,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
