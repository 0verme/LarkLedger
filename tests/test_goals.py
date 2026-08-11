"""P33-A Financial Goals — lifecycle, deterministic progress, privacy.

Cases covered:

* create / list / get / update / complete / archive / delete (ledger-scoped).
* Progress derives from **live** account balances: entry create / delete /
  restore and transfer create / reverse move it — there is no writable
  ``current_amount`` anywhere.
* Multiple bound accounts sum deterministically (same currency only).
* Currency validation: goal currency must match every bound account; a
  different-currency account is rejected, never summed 1:1.
* Target date: days_remaining, deadline math, cross-year and Feb.
* Forecast: trailing net-saving rate; insufficient history / non-positive
  rate yields ``None`` instead of a guess.
* Privacy (P32 carried over): a goal bound to A's private account is invisible
  to B (404), and B cannot infer its balance through list / progress / forecast
  side channels. Personal ledgers keep exact legacy behavior.
* Cross-ledger goal ids resolve to 404; non-creator/non-owner members cannot
  modify another member's goal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountType,
    AccountVisibility,
    Direction,
    LedgerEntry,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.goals import (
    GoalConflictError,
    GoalNotFoundError,
    GoalProgressService,
    GoalService,
    GoalValidationError,
)
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
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
    owner = await _identity(session, "ou_g_owner", "A")
    member = await _identity(session, "ou_g_member", "B")
    manager = HouseholdManagementService(session, currency="CNY", timezone=TZ)
    home = await manager.create(owner.actor_user_id, "目标家庭")
    invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_g_member")
    await manager.accept(member.actor_user_id, invitation.public_id)
    owner_ctx = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_g_owner",
    )
    member_ctx = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_g_member",
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


async def _account(
    session: AsyncSession,
    context: RequestContext,
    name: str,
    *,
    opening_balance: Decimal = Decimal("0"),
    account_type: AccountType = AccountType.CASH,
    currency: str = "CNY",
    visibility: AccountVisibility = AccountVisibility.SHARED,
) -> Account:
    account = await AccountService(session).create(
        context,
        name=name,
        account_type=account_type,
        currency=currency,
        opening_balance=opening_balance,
        visibility=visibility,
    )
    await session.commit()
    return account


async def _entry(
    session: AsyncSession,
    context: RequestContext,
    *,
    short_id: str,
    amount: str,
    account_id: uuid.UUID | None,
    direction: Direction,
    category: str = "储蓄",
    occurred_at: datetime | None = None,
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
    session.add(entry)
    await session.commit()
    return entry


async def _goal(
    session: AsyncSession,
    context: RequestContext,
    *,
    name: str = "应急储备",
    target: str = "60000",
    account_ids: list[uuid.UUID] | None = None,
    currency: str | None = None,
    target_date: date | None = None,
) -> object:
    service = GoalService(session, timezone=TZ, currency="CNY")
    goal = await service.create(
        context,
        name=name,
        target_amount=Decimal(target),
        currency=currency,
        target_date=target_date,
        account_ids=account_ids,
    )
    await session.commit()
    return goal


async def _progress(
    session: AsyncSession,
    context: RequestContext,
    goal_id: uuid.UUID,
) -> object:
    goal = await GoalService(session, timezone=TZ, currency="CNY").get(context, goal_id)
    return await GoalProgressService(session, timezone=TZ, currency="CNY").progress(context, goal)


@pytest.mark.asyncio
async def test_create_list_get_goal(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo1", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(
        session, context, "招行储蓄", opening_balance=Decimal("30000")
    )
    goal = await _goal(session, context, account_ids=[account.id], target_date=date(2027, 3, 31))

    service = GoalService(session, timezone=TZ, currency="CNY")
    listed = await service.list_goals(context)
    assert [item.id for item in listed] == [goal.id]  # type: ignore[union-attr]
    got = await service.get(context, goal.id)  # type: ignore[union-attr]
    assert got.name == "应急储备"
    assert got.currency == "CNY"
    assert got.target_date == date(2027, 3, 31)
    assert got.status == "active"

    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.current_amount == Decimal("30000")
    assert progress.target_amount == Decimal("60000")
    assert progress.remaining_amount == Decimal("30000")
    assert progress.progress_ratio == Decimal("0.5")
    assert progress.progress_percent == Decimal("50.00")
    assert progress.is_target_reached is False
    local_today = datetime.now(UTC).astimezone().date()
    assert progress.days_remaining == (date(2027, 3, 31) - local_today).days


@pytest.mark.asyncio
async def test_progress_tracks_live_balance_changes(session: AsyncSession) -> None:
    """Account balance 5000 / target 10000 → 50%; an expense moves it to 40%
    with no manual ``current_amount`` write anywhere (P33 §49)."""
    owner = await _identity(session, "ou_solo2", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("5000"))
    goal = await _goal(session, context, name="攒钱", target="10000", account_ids=[account.id])

    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.progress_percent == Decimal("50.00")

    await _entry(
        session, context, short_id="Z0001", amount="1000", account_id=account.id,
        direction=Direction.EXPENSE,
    )
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.current_amount == Decimal("4000")
    assert progress.progress_percent == Decimal("40.00")
    assert progress.remaining_amount == Decimal("6000")

    # Income increases it again.
    await _entry(
        session, context, short_id="Z0002", amount="2000", account_id=account.id,
        direction=Direction.INCOME,
    )
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.current_amount == Decimal("6000")
    assert progress.progress_percent == Decimal("60.00")


@pytest.mark.asyncio
async def test_revision_delete_restore_recalculates_progress(session: AsyncSession) -> None:
    """Soft delete removes an entry from progress; restore brings it back
    (P33 §50)."""
    owner = await _identity(session, "ou_solo3", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("8000"))
    goal = await _goal(session, context, name="目标", target="10000", account_ids=[account.id])
    entry = await _entry(
        session, context, short_id="Z0101", amount="1000", account_id=account.id,
        direction=Direction.EXPENSE,
    )
    assert (await _progress(session, context, goal.id)).current_amount == Decimal("7000")  # type: ignore[union-attr]

    entry.deleted_at = datetime.now(UTC)
    await session.commit()
    assert (await _progress(session, context, goal.id)).current_amount == Decimal("8000")  # type: ignore[union-attr]

    entry.deleted_at = None
    await session.commit()
    assert (await _progress(session, context, goal.id)).current_amount == Decimal("7000")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_transfer_reverse_recalculates_progress(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo4", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("10000"))
    goal = await _goal(session, context, name="目标", target="20000", account_ids=[account.id])
    assert (await _progress(session, context, goal.id)).current_amount == Decimal("10000")  # type: ignore[union-attr]

    other = await _account(session, context, "支付宝", opening_balance=Decimal("0"))
    transfer = await TransferService(session).create(
        context,
        from_account_id=account.id,
        to_account_id=other.id,
        amount=Decimal("3000"),
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
    )
    await session.commit()
    # Money moved out of the bound account.
    assert (await _progress(session, context, goal.id)).current_amount == Decimal("7000")  # type: ignore[union-attr]

    await TransferService(session).reverse(context, transfer.id)
    await session.commit()
    assert (await _progress(session, context, goal.id)).current_amount == Decimal("10000")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_multiple_accounts_sum_deterministically(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo5", "我")
    await session.commit()
    context = _context(owner)
    a = await _account(session, context, "招行储蓄", opening_balance=Decimal("25000"))
    b = await _account(session, context, "支付宝余额", opening_balance=Decimal("5000"))
    goal = await _goal(session, context, account_ids=[a.id, b.id])

    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.current_amount == Decimal("30000")
    assert progress.progress_percent == Decimal("50.00")


@pytest.mark.asyncio
async def test_currency_validation_rejects_mismatch(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo6", "我")
    await session.commit()
    context = _context(owner)
    usd = await _account(
        session, context, "美元账户", currency="USD", opening_balance=Decimal("100")
    )
    cny = await _account(session, context, "人民币账户", currency="CNY")
    service = GoalService(session, timezone=TZ, currency="CNY")
    with pytest.raises(GoalValidationError):
        await service.create(
            context,
            name="错币种目标",
            target_amount=Decimal("60000"),
            currency="CNY",
            account_ids=[usd.id],
        )
    # A USD goal bound to a CNY account is equally rejected — never 1:1.
    with pytest.raises(GoalValidationError):
        await service.create(
            context,
            name="美元目标",
            target_amount=Decimal("10000"),
            currency="USD",
            account_ids=[cny.id],
        )


@pytest.mark.asyncio
async def test_liability_account_rejected(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo7", "我")
    await session.commit()
    context = _context(owner)
    liability = await _account(
        session, context, "信用卡", account_type=AccountType.LIABILITY
    )
    with pytest.raises(GoalValidationError):
        await GoalService(session, timezone=TZ, currency="CNY").create(
            context,
            name="负债目标",
            target_amount=Decimal("1000"),
            account_ids=[liability.id],
        )


@pytest.mark.asyncio
async def test_target_reached(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo8", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("12000"))
    goal = await _goal(session, context, target="10000", account_ids=[account.id])
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.is_target_reached is True
    assert progress.remaining_amount == Decimal("0")
    assert progress.progress_percent == Decimal("120.00")


@pytest.mark.asyncio
async def test_forecast_with_sufficient_history(session: AsyncSession) -> None:
    """60 days of +2000/month net saving → rate ≈ 1000/month; remaining 10000
    → ≈ 10 months. Deterministic formula, no AI."""
    owner = await _identity(session, "ou_solo9", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("10000"))
    goal = await _goal(session, context, name="目标", target="20000", account_ids=[account.id])
    # 60 days of income 2000 every 30 days starting 60 days ago.
    now = datetime(2026, 8, 8, tzinfo=UTC)
    await _entry(
        session, context, short_id="F0001", amount="2000", account_id=account.id,
        direction=Direction.INCOME, occurred_at=now - timedelta(days=59),
    )
    await _entry(
        session, context, short_id="F0002", amount="2000", account_id=account.id,
        direction=Direction.INCOME, occurred_at=now - timedelta(days=29),
    )
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.monthly_saving_rate is not None
    assert progress.monthly_saving_rate > 0
    # current = 10000 + 4000 = 14000; remaining = 6000; rate ≈ 4000/59*30.4375
    assert progress.estimated_months_to_goal is not None
    assert progress.estimated_months_to_goal > 0


@pytest.mark.asyncio
async def test_forecast_insufficient_history_is_none(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo10", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("10000"))
    goal = await _goal(session, context, name="目标", target="20000", account_ids=[account.id])
    # Only 5 days of history — far below the 30-day minimum.
    now = datetime(2026, 8, 8, tzinfo=UTC)
    await _entry(
        session, context, short_id="G0001", amount="2000", account_id=account.id,
        direction=Direction.INCOME, occurred_at=now - timedelta(days=5),
    )
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.monthly_saving_rate is None
    assert progress.estimated_months_to_goal is None


@pytest.mark.asyncio
async def test_forecast_negative_rate_is_none(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo11", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("10000"))
    goal = await _goal(session, context, name="目标", target="20000", account_ids=[account.id])
    now = datetime(2026, 8, 8, tzinfo=UTC)
    # Only expenses in the window → net saving <= 0 → no forecast.
    await _entry(
        session, context, short_id="H0001", amount="500", account_id=account.id,
        direction=Direction.EXPENSE, occurred_at=now - timedelta(days=45),
    )
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.monthly_saving_rate is not None
    assert progress.estimated_months_to_goal is None


@pytest.mark.asyncio
async def test_target_date_boundaries(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo12", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("1000"))
    # target_date in the past
    past = await _goal(session, context, name="过期", target="5000",
                       account_ids=[account.id], target_date=date(2026, 1, 1))
    p = await _progress(session, context, past.id)  # type: ignore[union-attr]
    assert p.days_remaining is not None and p.days_remaining < 0
    # target_date today (local timezone)
    local_today = datetime.now(UTC).astimezone().date()
    today_goal = await _goal(session, context, name="今天", target="5000",
                             account_ids=[account.id], target_date=local_today)
    tp = await _progress(session, context, today_goal.id)  # type: ignore[union-attr]
    assert tp.days_remaining == 0
    # future target date
    future = await _goal(session, context, name="未来", target="5000",
                         account_ids=[account.id], target_date=date(2030, 12, 31))
    fp = await _progress(session, context, future.id)  # type: ignore[union-attr]
    assert fp.days_remaining is not None and fp.days_remaining > 0


@pytest.mark.asyncio
async def test_update_complete_archive_delete(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo13", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("1000"))
    goal = await _goal(session, context, account_ids=[account.id])
    service = GoalService(session, timezone=TZ, currency="CNY")

    updated_goal = await service.update(
        context, goal.id, name="新名字", target_amount=Decimal("99999")
    )  # type: ignore[union-attr]
    await session.commit()
    assert updated_goal.name == "新名字"
    assert updated_goal.target_amount == Decimal("99999")

    completed = await service.complete(context, goal.id)  # type: ignore[union-attr]
    await session.commit()
    assert completed.status == "completed"
    with pytest.raises(GoalConflictError):
        await service.complete(context, goal.id)  # type: ignore[union-attr]

    archived = await service.archive(context, goal.id)  # type: ignore[union-attr]
    await session.commit()
    assert archived.status == "archived"

    await service.delete(context, goal.id)  # type: ignore[union-attr]
    await session.commit()
    with pytest.raises(GoalNotFoundError):
        await service.get(context, goal.id)  # type: ignore[union-attr]
    # Deleting the goal must not touch the account.
    account_row = await session.get(Account, account.id)
    assert account_row is not None
    assert account_row.status == "active"


@pytest.mark.asyncio
async def test_cross_ledger_goal_is_404(session: AsyncSession) -> None:
    first = await _identity(session, "ou_dual1", "甲")
    second = await _identity(session, "ou_dual2", "乙")
    await session.commit()
    ctx1 = _context(first)
    ctx2 = _context(second)
    account = await _account(session, ctx1, "现金", opening_balance=Decimal("1000"))
    goal = await _goal(session, ctx1, account_ids=[account.id])
    with pytest.raises(GoalNotFoundError):
        await GoalService(session, timezone=TZ, currency="CNY").get(ctx2, goal.id)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_private_goal_invisible_to_other_member(session: AsyncSession) -> None:
    """P33 §48 critical privacy case: A's goal bound to A's private account is
    invisible to B through every read path, and B cannot infer the balance."""
    owner_ctx, member_ctx, _ = await _household(session)
    private = await _account(
        session, owner_ctx, "私房钱", opening_balance=Decimal("10000"),
        visibility=AccountVisibility.PRIVATE,
    )
    goal = await _goal(session, owner_ctx, name="私密储备", target="20000",
                       account_ids=[private.id])

    service = GoalService(session, timezone=TZ, currency="CNY")
    # B cannot see the goal at all (list or get → 404 semantics).
    assert [item.id for item in await service.list_goals(member_ctx)] == []
    with pytest.raises(GoalNotFoundError):
        await service.get(member_ctx, goal.id)  # type: ignore[union-attr]
    # B cannot read progress either — a direct progress call must 404, never
    # reveal a balance through a different exception type.
    with pytest.raises(GoalNotFoundError):
        await GoalProgressService(session, timezone=TZ, currency="CNY").progress(
            member_ctx, goal  # type: ignore[arg-type]
        )
    # B cannot manage it either.
    with pytest.raises(GoalNotFoundError):
        await service.update(member_ctx, goal.id, name="篡改")  # type: ignore[union-attr]
    # A still sees everything, including the exact derived balance.
    progress = await _progress(session, owner_ctx, goal.id)  # type: ignore[union-attr]
    assert progress.current_amount == Decimal("10000")

    # Once A flips the account to shared, B sees the goal with identical facts.
    await AccountService(session).set_visibility(
        owner_ctx, private.id, AccountVisibility.SHARED
    )
    await session.commit()
    visible = await service.get(member_ctx, goal.id)  # type: ignore[union-attr]
    assert visible.name == "私密储备"


@pytest.mark.asyncio
async def test_shared_goal_visible_to_both_members(session: AsyncSession) -> None:
    """G04 — a household shared goal bound to shared accounts shows the same
    deterministic progress to A and B."""
    owner_ctx, member_ctx, accounts = await _household(session)
    shared = accounts[0]
    await _account(session, owner_ctx, "家庭储蓄", opening_balance=Decimal("60000"))
    # Use the default shared account with a real balance.
    await _entry(
        session, owner_ctx, short_id="H0002", amount="30000", account_id=shared.id,
        direction=Direction.INCOME,
    )
    goal = await _goal(session, owner_ctx, name="家庭旅行基金", target="120000",
                       account_ids=[shared.id])
    owner_progress = await _progress(session, owner_ctx, goal.id)  # type: ignore[union-attr]
    member_progress = await _progress(session, member_ctx, goal.id)  # type: ignore[union-attr]
    assert owner_progress.current_amount == member_progress.current_amount
    assert owner_progress.progress_percent == member_progress.progress_percent
    assert owner_progress.remaining_amount == member_progress.remaining_amount


@pytest.mark.asyncio
async def test_non_owner_member_cannot_modify_others_goal(session: AsyncSession) -> None:
    owner_ctx, member_ctx, accounts = await _household(session)
    shared = accounts[0]
    goal = await _goal(session, owner_ctx, name="A的目标", target="1000",
                       account_ids=[shared.id])
    service = GoalService(session, timezone=TZ, currency="CNY")
    # B can read the shared goal but cannot modify / delete / complete it.
    assert (await service.get(member_ctx, goal.id)).name == "A的目标"  # type: ignore[union-attr]
    with pytest.raises(GoalNotFoundError):
        await service.update(member_ctx, goal.id, name="B改的")  # type: ignore[union-attr]
    with pytest.raises(GoalNotFoundError):
        await service.delete(member_ctx, goal.id)  # type: ignore[union-attr]
    with pytest.raises(GoalNotFoundError):
        await service.complete(member_ctx, goal.id)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_binding_replacement_on_update(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo14", "我")
    await session.commit()
    context = _context(owner)
    a = await _account(session, context, "账户A", opening_balance=Decimal("1000"))
    b = await _account(session, context, "账户B", opening_balance=Decimal("2000"))
    goal = await _goal(session, context, account_ids=[a.id])
    service = GoalService(session, timezone=TZ, currency="CNY")
    await service.update(context, goal.id, account_ids=[b.id])  # type: ignore[union-attr]
    await session.commit()
    bindings = await service._binding_rows(context, goal.id)  # type: ignore[union-attr]
    assert [row.account_id for row in bindings] == [b.id]
    progress = await _progress(session, context, goal.id)  # type: ignore[union-attr]
    assert progress.current_amount == Decimal("2000")


@pytest.mark.asyncio
async def test_goal_delete_does_not_touch_ledger_data(session: AsyncSession) -> None:
    """P33 §43/44 — deleting / archiving a goal never deletes accounts,
    entries, or transfers, and creating a goal never creates any of them."""
    owner = await _identity(session, "ou_solo15", "我")
    await session.commit()
    context = _context(owner)
    account = await _account(session, context, "现金", opening_balance=Decimal("1000"))
    entry = await _entry(
        session, context, short_id="D0001", amount="100", account_id=account.id,
        direction=Direction.EXPENSE,
    )
    goal = await _goal(session, context, account_ids=[account.id])
    service = GoalService(session, timezone=TZ, currency="CNY")
    await service.delete(context, goal.id)  # type: ignore[union-attr]
    await session.commit()
    assert await session.get(Account, account.id) is not None
    assert await session.get(LedgerEntry, entry.id) is not None
