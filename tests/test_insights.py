"""P33-B Deterministic Insights — rules, thresholds, privacy side-channels.

Cases covered:

* I01 spending change — exact current / baseline / change_percent from fixed
  test data; transfer, income, pending and deleted entries excluded.
* Zero baseline — never division by zero / infinite %; deterministic text.
* I02 budget risk — usage > elapsed + margin fires; below does not.
* I03 upcoming recurring — active expense rules within 30 days, grouped by
  currency (mixed currencies never summed).
* I04 goal progress — reached / deadline-soon / shortfall insights from the
  deterministic forecast.
* No-data / insufficient-history → ``[]`` (no forced noise).
* Privacy side-channel (P33 §27): A's private 500 never changes B's insights,
  and A's private goal never leaks into B's goal insights.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountType,
    AccountVisibility,
    Direction,
    LedgerEntry,
    RecurringFrequency,
    RecurringRule,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.budget import BudgetService
from lark_ledger.services.goals import GoalService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.insights import InsightPolicy, InsightService
from lark_ledger.services.recurring import RecurringService
from lark_ledger.services.transfers import TransferService

TZ = "Asia/Shanghai"


async def _identity(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone=TZ
    ).resolve_or_bootstrap(
        channel="feishu", external_subject_id=open_id, display_name=name
    )


async def _household(
    session: AsyncSession,
) -> tuple[RequestContext, RequestContext, list[Account]]:
    owner = await _identity(session, "ou_i_owner", "A")
    member = await _identity(session, "ou_i_member", "B")
    manager = HouseholdManagementService(session, currency="CNY", timezone=TZ)
    home = await manager.create(owner.actor_user_id, "洞察家庭")
    invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_i_member")
    await manager.accept(member.actor_user_id, invitation.public_id)
    owner_ctx = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_i_owner",
    )
    member_ctx = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_i_member",
    )
    accounts = await AccountService(session).list(owner_ctx, include_archived=True)
    await session.commit()
    return owner_ctx, member_ctx, accounts


def _context(actor: RequestContext) -> RequestContext:
    return RequestContext(
        actor_user_id=actor.actor_user_id,
        ledger_id=actor.ledger_id,
        source_channel="feishu",
        external_subject_id=actor.external_subject_id,
    )


async def _entry(
    session: AsyncSession,
    context: RequestContext,
    *,
    short_id: str,
    amount: str,
    category: str,
    direction: Direction = Direction.EXPENSE,
    account_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    deleted: bool = False,
) -> LedgerEntry:
    entry = LedgerEntry(
        user_open_id=context.external_subject_id or "ou",
        created_by_user_id=context.actor_user_id,
        paid_by_user_id=context.actor_user_id,
        ledger_id=context.ledger_id,
        account_id=account_id,
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=direction,
        category=category,
        note="",
        occurred_at=occurred_at or datetime(2026, 8, 8, 4, tzinfo=UTC),
        source_type="text",
    )
    if deleted:
        entry.deleted_at = datetime.now(UTC)
    session.add(entry)
    await session.commit()
    return entry


async def _account(
    session: AsyncSession,
    context: RequestContext,
    name: str,
    *,
    opening_balance: Decimal = Decimal("0"),
    visibility: AccountVisibility = AccountVisibility.SHARED,
) -> Account:
    account = await AccountService(session).create(
        context,
        name=name,
        account_type=AccountType.CASH,
        currency="CNY",
        opening_balance=opening_balance,
        visibility=visibility,
    )
    await session.commit()
    return account


async def _insights(
    session: AsyncSession,
    context: RequestContext,
    *,
    period: date | None = None,
    now: datetime | None = None,
    policy: InsightPolicy | None = None,
) -> list:
    return await InsightService(
        session, timezone=TZ, currency="CNY", policy=policy
    ).insights(context, period=period, now=now)


# -- I01 spending change -----------------------------------------------------


@pytest.mark.asyncio
async def test_i01_spending_change_exact_numbers(session: AsyncSession) -> None:
    """P33 §51: history 1000/1000/1000, current 1500 → baseline 1000,
    change +500, change_percent 50% — computed, never by AI."""
    owner = await _identity(session, "ou_i01", "我")
    await session.commit()
    context = _context(owner)
    now = datetime(2026, 8, 8, tzinfo=UTC)
    for month_offset, amount in ((3, "1000"), (2, "1000"), (1, "1000")):
        await _entry(
            session, context, short_id=f"H{month_offset}", amount=amount,
            category="餐饮",
            occurred_at=now - timedelta(days=30 * month_offset),
        )
    await _entry(
        session, context, short_id="C1", amount="1500", category="餐饮",
        occurred_at=now - timedelta(days=1),
    )
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    change = [
        item for item in insights
        if item.type == "spending_change" and item.related_category == "餐饮"
    ]
    assert len(change) == 1
    assert change[0].metric["current"] == "1500.00"
    assert change[0].metric["baseline"] == "1000.00"
    assert change[0].metric["change"] == "500.00"
    assert change[0].metric["change_percent"] == "50.0"
    assert change[0].severity == "attention"


@pytest.mark.asyncio
async def test_i01_income_transfer_pending_deleted_excluded(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_i01b", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("100"))
    other = await _account(session, context, "支付宝")
    now = datetime(2026, 8, 8, tzinfo=UTC)
    # Baseline month: one 餐饮 expense.
    await _entry(session, context, short_id="B1", amount="1000", category="餐饮",
                 occurred_at=now - timedelta(days=40), account_id=account.id)
    # Current month: income, transfer, deleted expense, real expense.
    await _entry(
        session, context, short_id="I1", amount="5000", category="餐饮",
        direction=Direction.INCOME, occurred_at=now - timedelta(days=2),
        account_id=account.id,
    )
    await TransferService(session).create(
        context, from_account_id=account.id, to_account_id=other.id,
        amount=Decimal("999"), occurred_at=now - timedelta(days=2),
    )
    await session.commit()
    await _entry(
        session, context, short_id="D1", amount="3000", category="餐饮",
        occurred_at=now - timedelta(days=1), account_id=account.id, deleted=True,
    )
    await _entry(
        session, context, short_id="R1", amount="1500", category="餐饮",
        occurred_at=now - timedelta(days=1), account_id=account.id,
    )
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    change = [
        item for item in insights
        if item.type == "spending_change" and item.related_category == "餐饮"
    ]
    assert len(change) == 1
    # Only the live 1500 expense counts — income/transfer/deleted never do.
    assert change[0].metric["current"] == "1500.00"


@pytest.mark.asyncio
async def test_i01_zero_baseline_no_division_by_zero(session: AsyncSession) -> None:
    """P33 §52: history has other categories but 0 for this one; current 1000
    → deterministic "new category" text, never division by zero / 999999%."""
    owner = await _identity(session, "ou_i01c", "我")
    await session.commit()
    context = _context(owner)
    now = datetime(2026, 8, 8, tzinfo=UTC)
    # History: 餐饮 only — 健身 has a zero baseline.
    for month_offset, amount in ((3, "1000"), (2, "1000"), (1, "1000")):
        await _entry(
            session, context, short_id=f"Z{month_offset}", amount=amount,
            category="餐饮", occurred_at=now - timedelta(days=30 * month_offset),
        )
    # Current month: brand-new 健身 spending appears.
    await _entry(
        session, context, short_id="N1", amount="1000", category="健身",
        occurred_at=now - timedelta(days=1),
    )
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    new = [
        item for item in insights
        if item.type == "spending_change" and item.related_category == "健身"
    ]
    assert len(new) == 1
    assert new[0].severity == "info"
    assert "新的健身支出" in new[0].summary
    assert "999999" not in new[0].summary
    assert "Infinity" not in new[0].summary


@pytest.mark.asyncio
async def test_i01_below_threshold_no_insight(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_i01d", "我")
    await session.commit()
    context = _context(owner)
    now = datetime(2026, 8, 8, tzinfo=UTC)
    for month_offset, amount in ((3, "1000"), (2, "1000"), (1, "1000")):
        await _entry(
            session, context, short_id=f"L{month_offset}", amount=amount,
            category="餐饮", occurred_at=now - timedelta(days=30 * month_offset),
        )
    # +20% change is below the 30% threshold.
    await _entry(
        session, context, short_id="L4", amount="1200", category="餐饮",
        occurred_at=now - timedelta(days=1),
    )
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    assert [item for item in insights if item.type == "spending_change"] == []


@pytest.mark.asyncio
async def test_i01_insufficient_history_no_change_insight(session: AsyncSession) -> None:
    """Only 5 days of history → no baseline comparison, no forced insight."""
    owner = await _identity(session, "ou_i01e", "我")
    await session.commit()
    context = _context(owner)
    now = datetime(2026, 8, 8, tzinfo=UTC)
    await _entry(session, context, short_id="M1", amount="1000", category="餐饮",
                 occurred_at=now - timedelta(days=4))
    await _entry(session, context, short_id="M2", amount="2000", category="餐饮",
                 occurred_at=now - timedelta(days=1))
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    assert [item for item in insights if item.type == "spending_change"] == []


# -- I02 budget risk ---------------------------------------------------------


@pytest.mark.asyncio
async def test_i02_budget_risk_fires_above_margin(session: AsyncSession) -> None:
    """P33 §53: day 10/30 (elapsed 33%); budget 3000 spent 2100 (70%) → fires;
    spent 900 (30%) → does not."""
    owner = await _identity(session, "ou_i02", "我")
    await session.commit()
    context = _context(owner)
    # day 10 of August 2026.
    now = datetime(2026, 8, 10, 4, tzinfo=UTC)
    await _entry(session, context, short_id="B1", amount="2100", category="餐饮",
                 occurred_at=now - timedelta(days=1))
    await BudgetService(session, currency="CNY", timezone=TZ).set_category_budget(
        context, period=date(2026, 8, 1), category="餐饮", amount=Decimal("3000")
    )
    await session.commit()
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    risk = [item for item in insights if item.type == "budget_risk"]
    assert len(risk) == 1
    assert risk[0].metric["usage_rate"] == "70.00"
    assert risk[0].metric["elapsed_ratio"] == "32.26"
    assert risk[0].severity == "warning"


@pytest.mark.asyncio
async def test_i02_budget_risk_below_margin_no_insight(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_i02b", "我")
    await session.commit()
    context = _context(owner)
    now = datetime(2026, 8, 10, 4, tzinfo=UTC)
    await _entry(session, context, short_id="C1", amount="900", category="餐饮",
                 occurred_at=now - timedelta(days=1))
    await BudgetService(session, currency="CNY", timezone=TZ).set_category_budget(
        context, period=date(2026, 8, 1), category="餐饮", amount=Decimal("3000")
    )
    await session.commit()
    insights = await _insights(session, context, period=date(2026, 8, 1), now=now)
    assert [item for item in insights if item.type == "budget_risk"] == []


# -- I03 upcoming recurring --------------------------------------------------


@pytest.mark.asyncio
async def test_i03_upcoming_recurring_grouped_by_currency(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_i03", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金")
    service = RecurringService(session, currency="CNY", timezone=TZ)
    today = datetime(2026, 8, 8, 4, tzinfo=UTC).date()
    await service.create(
        context, transaction_type=Direction.EXPENSE, amount=Decimal("88"), currency=None,
        category="娱乐", description="Netflix", frequency=RecurringFrequency.MONTHLY,
        interval=1, next_occurrence=today + timedelta(days=5), account_id=account.id,
    )
    await service.create(
        context, transaction_type=Direction.EXPENSE, amount=Decimal("3500"), currency=None,
        category="居住", description="房租", frequency=RecurringFrequency.MONTHLY,
        interval=1, next_occurrence=today + timedelta(days=12), account_id=account.id,
    )
    # Disabled rule outside the window must never count.
    await service.create(
        context, transaction_type=Direction.EXPENSE, amount=Decimal("999999"), currency=None,
        category="测试", description="禁用项", frequency=RecurringFrequency.MONTHLY,
        interval=1, next_occurrence=today + timedelta(days=3), account_id=account.id,
    )
    await session.commit()
    disabled = list(
        (
            await session.scalars(
                select(RecurringRule).where(RecurringRule.description == "禁用项")
            )
        ).all()
    )
    await RecurringService(session, currency="CNY", timezone=TZ).disable(
        context, disabled[0].id
    )
    await session.commit()

    insights = await _insights(session, context, now=datetime(2026, 8, 8, tzinfo=UTC))
    upcoming = [item for item in insights if item.type == "upcoming_recurring"]
    assert len(upcoming) == 1
    assert upcoming[0].metric["count"] == "2"
    assert upcoming[0].metric["amount_CNY"] == "3588.00"


@pytest.mark.asyncio
async def test_i03_mixed_currency_never_summed(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_i03b", "我")
    await session.commit()
    context = _context(owner)
    cny = await _account(session, context, "人民币")
    usd = await _account(session, context, "美元", opening_balance=Decimal("1"))
    # Swap USD account currency via direct update (AccountService enforces ledger
    # currency for transfers only; rules may hold any 3-letter code).
    usd.currency = "USD"
    await session.commit()
    service = RecurringService(session, currency="CNY", timezone=TZ)
    today = datetime(2026, 8, 8, 4, tzinfo=UTC).date()
    await service.create(
        context, transaction_type=Direction.EXPENSE, amount=Decimal("88"), currency=None,
        category="娱乐", description="Netflix", frequency=RecurringFrequency.MONTHLY,
        interval=1, next_occurrence=today + timedelta(days=5), account_id=cny.id,
    )
    await service.create(
        context, transaction_type=Direction.EXPENSE, amount=Decimal("10"), currency="USD",
        category="娱乐", description="iCloud", frequency=RecurringFrequency.MONTHLY,
        interval=1, next_occurrence=today + timedelta(days=6), account_id=usd.id,
    )
    await session.commit()
    insights = await _insights(session, context, now=datetime(2026, 8, 8, tzinfo=UTC))
    upcoming = [item for item in insights if item.type == "upcoming_recurring"]
    assert len(upcoming) == 1
    metric = upcoming[0].metric
    assert metric["count"] == "2"
    assert metric["amount_CNY"] == "88.00"
    assert metric["amount_USD"] == "10.00"
    # Never 88 + 10 = 98 in one bucket.
    assert "amount_CNY" in metric and "amount_USD" in metric


# -- I04 goal progress -------------------------------------------------------


@pytest.mark.asyncio
async def test_i04_goal_reached_and_shortfall_insights(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_i04", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("5000"))
    now = datetime(2026, 8, 8, tzinfo=UTC)
    # Goal A: reached (balance 5000 >= target 3000).
    goal_a = await GoalService(session, timezone=TZ, currency="CNY").create(
        context, name="小目标", target_amount=Decimal("3000"), account_ids=[account.id],
    )
    # Goal B: far from target, deadline in 10 days, tiny saving rate → shortfall.
    goal_b = await GoalService(session, timezone=TZ, currency="CNY").create(
        context, name="大目标", target_amount=Decimal("100000"), account_ids=[account.id],
        target_date=date(2026, 8, 18),
    )
    # Trailing 45 days of small savings → rate > 0 but nowhere near 95000 in 10 days.
    await _entry(session, context, short_id="S1", amount="100", category="工资",
                 direction=Direction.INCOME, occurred_at=now - timedelta(days=45))
    await session.commit()
    insights = await _insights(session, context, now=now)
    reached = [item for item in insights if item.related_goal == str(goal_a.id)]
    assert len(reached) == 1
    assert reached[0].severity == "info"
    shortfall = [item for item in insights if item.related_goal == str(goal_b.id)]
    assert len(shortfall) == 1
    assert shortfall[0].severity == "warning"
    assert Decimal(shortfall[0].metric["projected_shortfall"]) > 0


# -- no-data & privacy -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_data_returns_empty(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_empty", "我")
    await session.commit()
    context = _context(owner)
    insights = await _insights(session, context, now=datetime(2026, 8, 8, tzinfo=UTC))
    assert insights == []


@pytest.mark.asyncio
async def test_no_budget_no_recurring_no_goal_empty(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_empty2", "我")
    await session.commit()
    context = _context(owner)
    await _entry(session, context, short_id="E1", amount="50", category="餐饮")
    insights = await _insights(session, context, period=date(2026, 8, 1))
    # Small single expense without history → no forced insight.
    assert [item for item in insights if item.type == "spending_change"] == []


@pytest.mark.asyncio
async def test_privacy_side_channel_insights(session: AsyncSession) -> None:
    """P33 §27 & §48: A's private 500 and private goal must never change B's
    insights — no category totals, no budget spend, no goal progress."""
    owner_ctx, member_ctx, _ = await _household(session)
    private = await _account(
        session, owner_ctx, "私房钱", opening_balance=Decimal("10000"),
        visibility=AccountVisibility.PRIVATE,
    )
    now = datetime(2026, 8, 8, tzinfo=UTC)
    # A: private spending that jumps this month (baseline 500 → current 1500).
    for month_offset, amount in ((3, "500"), (2, "500"), (1, "500")):
        await _entry(session, owner_ctx, short_id=f"P{month_offset}", amount=amount,
                     category="私人购物", account_id=private.id,
                     occurred_at=now - timedelta(days=30 * month_offset))
    await _entry(session, owner_ctx, short_id="P4", amount="1500", category="私人购物",
                 account_id=private.id, occurred_at=now - timedelta(days=1))
    # A: private goal already reached (balance 7000 >= target 5000).
    private_goal = await GoalService(session, timezone=TZ, currency="CNY").create(
        owner_ctx, name="私密目标", target_amount=Decimal("5000"), account_ids=[private.id],
    )
    await session.commit()

    owner_insights = await _insights(session, owner_ctx, period=date(2026, 8, 1), now=now)
    member_insights = await _insights(session, member_ctx, period=date(2026, 8, 1), now=now)
    # Owner sees their private category change and their private goal reached.
    assert any(item.related_category == "私人购物" for item in owner_insights)
    assert any(item.related_goal == str(private_goal.id) for item in owner_insights)
    # Member sees nothing about A's private data — no side channel.
    assert all(item.related_category != "私人购物" for item in member_insights)
    assert all(item.related_goal != str(private_goal.id) for item in member_insights)
    # The member's summary numbers cannot reveal the private amounts either.
    for item in member_insights:
        assert "1500" not in item.summary
        assert "私密目标" not in item.summary
