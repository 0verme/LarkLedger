"""P31 Household Overview — deterministic ledger home view."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import AccountType, Direction, LedgerEntry, RecurringFrequency
from lark_ledger.overview_commands import try_parse_overview_command
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.household_overview import HouseholdOverviewService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.recurring import RecurringService
from lark_ledger.services.transfers import TransferService


async def _identity(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(
        channel="feishu", external_subject_id=open_id, display_name=name
    )


async def _household(
    session: AsyncSession,
) -> tuple[RequestContext, RequestContext]:
    owner = await _identity(session, "ou_owner", "A")
    member = await _identity(session, "ou_member", "B")
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    home = await manager.create(owner.actor_user_id, "测试家庭")
    invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_member")
    await manager.accept(member.actor_user_id, invitation.public_id)
    await session.commit()
    owner_ctx = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_owner",
    )
    member_ctx = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_member",
    )
    return owner_ctx, member_ctx


async def _record(
    session: AsyncSession,
    context: RequestContext,
    *,
    amount: str,
    direction: Direction = Direction.EXPENSE,
    category: str = "餐饮",
    payer: str | None = None,
) -> LedgerEntry:
    result = await LedgerService(session, commit_changes=False).execute(
        context,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal(amount),
            direction=direction,
            category=category,
            note="测试",
            occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
            payer_reference=payer,
        ),
    )
    await session.commit()
    assert result.entry_id is not None
    entry = await session.get(LedgerEntry, result.entry_id)
    assert entry is not None
    return entry


@pytest.mark.asyncio
async def test_overview_household_aggregates_deterministically(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx = await _household(session)
    await _record(session, owner_ctx, amount="100", category="餐饮")
    await _record(session, member_ctx, amount="200", category="交通", payer="B")
    await _record(
        session, owner_ctx, amount="500", direction=Direction.INCOME, category="工资"
    )
    # A transfer must never appear in income / expense / budget.
    accounts = await AccountService(session).list(owner_ctx, include_archived=True)
    second = await AccountService(session).create(
        owner_ctx, name="钱包", account_type=AccountType.CASH, currency="CNY"
    )
    await TransferService(session).create(
        owner_ctx,
        from_account_id=accounts[0].id,
        to_account_id=second.id,
        amount=Decimal("10"),
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
    )
    await session.commit()
    # A total budget for the period.
    from lark_ledger.services.budget import BudgetService

    await BudgetService(session, currency="CNY", timezone="Asia/Shanghai").set_total_budget(
        owner_ctx, period=date(2026, 8, 1), amount=Decimal("1000")
    )
    await session.commit()
    # An upcoming recurring rule.
    await RecurringService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).create(
        owner_ctx,
        transaction_type=Direction.EXPENSE,
        amount=Decimal("300"),
        currency=None,
        category="居住",
        description="房租",
        frequency=RecurringFrequency.MONTHLY,
        interval=1,
        next_occurrence=date(2099, 9, 1),
        account_id=accounts[0].id,
    )
    await session.commit()

    overview = await HouseholdOverviewService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).overview(owner_ctx, period=date(2026, 8, 1))

    assert overview.ledger_name == "测试家庭公共账本"
    assert overview.period == "2026-08"
    assert overview.income_total == Decimal("500")
    assert overview.expense_total == Decimal("300")  # transfer excluded
    assert overview.net_total == Decimal("200")
    assert overview.budget.total_budget == Decimal("1000")
    assert overview.budget.total_spent == Decimal("300")
    assert overview.budget.usage_rate == Decimal("30.00")

    contributions = {item.user_id: item for item in overview.member_contributions}
    assert contributions[str(owner_ctx.actor_user_id)].expense_total == Decimal("100")
    assert contributions[str(member_ctx.actor_user_id)].expense_total == Decimal("200")

    categories = {item.category: item for item in overview.top_categories}
    assert categories["餐饮"].amount == Decimal("100")
    assert categories["交通"].amount == Decimal("200")

    assert [item.description for item in overview.upcoming_recurring] == ["房租"]

    recent_ids = [item.id for item in overview.recent_transactions]
    assert len(recent_ids) == 3


async def test_overview_personal_ledger_single_owner(
    session: AsyncSession,
) -> None:
    owner = await _identity(session, "ou_personal", "我")
    await session.commit()
    context = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="feishu",
        external_subject_id="ou_personal",
    )
    await _record(session, context, amount="80")
    overview = await HouseholdOverviewService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).overview(context, period=date(2026, 8, 1))
    assert overview.expense_total == Decimal("80")
    assert len(overview.member_contributions) == 1
    assert overview.member_contributions[0].display_name == "我"


async def test_pending_recurring_not_counted_until_confirmed(
    session: AsyncSession,
) -> None:
    owner_ctx, _ = await _household(session)
    accounts = await AccountService(session).list(owner_ctx)
    rule = await RecurringService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).create(
        owner_ctx,
        transaction_type=Direction.EXPENSE,
        amount=Decimal("999"),
        currency=None,
        category="保险",
        description="保险",
        frequency=RecurringFrequency.YEARLY,
        interval=1,
        next_occurrence=date(2099, 1, 1),
        account_id=accounts[0].id,
    )
    await session.commit()
    # Generate the pending but do not confirm.
    from lark_ledger.config import Settings
    from lark_ledger.services.pending import PendingCommandStore

    settings = Settings(
        _env_file=None,
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
        pending_expires_seconds=3600,
        currency="CNY",
        timezone="Asia/Shanghai",
    )
    store = PendingCommandStore(session, settings)
    await store.create_recurring_pending(
        session=session,
        context=owner_ctx,
        user_open_id="ou_owner",
        rule=rule,
        occurrence_date=date(2099, 1, 1),
        now=datetime(2099, 1, 1, 0, tzinfo=UTC),
    )
    await session.commit()

    overview = await HouseholdOverviewService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).overview(owner_ctx, period=date(2026, 8, 1))
    assert overview.expense_total == Decimal("0")
    # The rule itself still shows as upcoming.
    assert len(overview.upcoming_recurring) == 1


async def test_overview_command_parser() -> None:
    assert try_parse_overview_command("概览") is not None
    assert try_parse_overview_command("家庭概览") is not None
    assert try_parse_overview_command("家庭开销") is not None
    assert try_parse_overview_command("午饭32") is None
    assert try_parse_overview_command("") is None
