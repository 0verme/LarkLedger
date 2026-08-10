"""P30 Household Contribution — payer attribution, member resolution, stats."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.confirmation_id import format_confirmation_ref
from lark_ledger.context import RequestContext
from lark_ledger.models import Direction, LedgerEntry, RecurringFrequency
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.member_resolution import (
    MemberResolutionService,
    PayerResolutionError,
)
from lark_ledger.services.member_stats import MemberStatsService
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.services.recurring import RecurringService
from lark_ledger.services.transfers import TransferService


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
        pending_expires_seconds=3600,
        currency="CNY",
        timezone="Asia/Shanghai",
    )


async def _identity(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(
        channel="feishu", external_subject_id=open_id, display_name=name
    )


async def _household(
    session: AsyncSession, owner_name: str, member_name: str
) -> tuple[RequestContext, RequestContext, object, object]:
    """Create a household with an owner + member, return (owner, member, home, shared_ledger)."""
    owner = await _identity(session, "ou_owner", owner_name)
    member = await _identity(session, "ou_member", member_name)
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    home = await manager.create(owner.actor_user_id, "测试家庭")
    invitation = await manager.invite(
        owner.actor_user_id, home.household.id, "ou_member"
    )
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
    return owner_ctx, member_ctx, home, owner


async def _record(
    session: AsyncSession, context: RequestContext, *, amount: str, payer: str | None = None
) -> LedgerEntry:
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal(amount),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
        payer_reference=payer,
    )
    result = await LedgerService(session, commit_changes=False).execute(context, command)
    await session.commit()
    assert result.entry_id is not None
    entry = await session.get(LedgerEntry, result.entry_id)
    assert entry is not None
    return entry


async def test_case_a_household_shared_access(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    # Both members can write to the shared ledger.
    a_entry = await _record(session, owner_ctx, amount="50")
    b_entry = await _record(session, member_ctx, amount="30")
    assert a_entry.ledger_id == owner_ctx.ledger_id == member_ctx.ledger_id
    assert b_entry.ledger_id == owner_ctx.ledger_id


async def test_case_b_default_payer_is_actor(
    session: AsyncSession,
) -> None:
    owner_ctx, _, _, _ = await _household(session, "A", "B")
    entry = await _record(session, owner_ctx, amount="50")
    assert entry.created_by_user_id == owner_ctx.actor_user_id
    assert entry.paid_by_user_id == owner_ctx.actor_user_id


async def test_case_c_payer_reference_resolves_to_member(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    entry = await _record(session, owner_ctx, amount="120", payer="B")
    assert entry.created_by_user_id == owner_ctx.actor_user_id
    assert entry.paid_by_user_id == member_ctx.actor_user_id


async def test_payer_reference_zero_matches_prompts(
    session: AsyncSession,
) -> None:
    owner_ctx, _, _, _ = await _household(session, "A", "B")
    resolver = MemberResolutionService(session)
    with pytest.raises(PayerResolutionError) as exc:
        await resolver.resolve_payer(owner_ctx, "不存在的人")
    assert "成员" in str(exc.value)


async def test_payer_reference_ambiguous_rejected(
    session: AsyncSession,
) -> None:
    owner_ctx, _, _, _ = await _household(session, "A", "佳佳")
    # Add a second member with the same display name.
    other = await _identity(session, "ou_other", "佳佳")
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    household_id = await _household_id(session, owner_ctx)
    invitation = await manager.invite(owner_ctx.actor_user_id, household_id, "ou_other")
    await manager.accept(other.actor_user_id, invitation.public_id)
    await session.commit()
    resolver = MemberResolutionService(session)
    with pytest.raises(PayerResolutionError):
        await resolver.resolve_payer(owner_ctx, "佳佳")


async def _household_id(session: AsyncSession, context: RequestContext) -> object:
    from lark_ledger.models import Ledger

    ledger = await session.get(Ledger, context.ledger_id)
    return ledger.household_id


async def test_alias_resolves_before_display_name(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    household_id = await _household_id(session, owner_ctx)
    membership = await manager.set_member_alias(
        owner_ctx.actor_user_id, household_id, member_ctx.actor_user_id, "老婆"
    )
    assert membership.alias == "老婆"
    await session.commit()

    resolver = MemberResolutionService(session)
    assert await resolver.resolve_payer(owner_ctx, "老婆") == member_ctx.actor_user_id
    assert await resolver.resolve_payer(owner_ctx, "B") == member_ctx.actor_user_id

    entry = await _record(session, owner_ctx, amount="120", payer="老婆")
    assert entry.paid_by_user_id == member_ctx.actor_user_id


async def test_alias_unique_per_household(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    other = await _identity(session, "ou_other", "C")
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    household_id = await _household_id(session, owner_ctx)
    await manager.set_member_alias(
        owner_ctx.actor_user_id, household_id, member_ctx.actor_user_id, "老婆"
    )
    invitation = await manager.invite(
        owner_ctx.actor_user_id, household_id, "ou_other"
    )
    await manager.accept(other.actor_user_id, invitation.public_id)
    await session.commit()
    from lark_ledger.services.household_management import HouseholdConflictError

    with pytest.raises(HouseholdConflictError):
        await manager.set_member_alias(
            owner_ctx.actor_user_id, household_id, other.actor_user_id, "老婆"
        )


async def test_payer_must_be_ledger_member(
    session: AsyncSession,
) -> None:
    owner_ctx, _, _, _ = await _household(session, "A", "B")
    outsider = await _identity(session, "ou_outside", "外部")
    with pytest.raises(PayerResolutionError):
        await MemberResolutionService(session).resolve_payer(owner_ctx, str(outsider.actor_user_id))


async def test_case_d_budget_counts_all_members_once(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    await _record(session, owner_ctx, amount="100")
    await _record(session, member_ctx, amount="200")
    from lark_ledger.services.budget import BudgetService

    overview = await BudgetService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).overview(owner_ctx, period=date(2026, 8, 1))
    assert overview.total_spent == Decimal("300")


async def test_member_stats_aggregate_by_payer_and_exclude_transfers(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    await _record(session, owner_ctx, amount="100")
    await _record(session, member_ctx, amount="200")
    # A records a B-paid expense too (created_by=A, paid_by=B).
    await _record(session, owner_ctx, amount="50", payer="B")
    # A transfer must never enter member stats.
    from lark_ledger.models import AccountType

    account_service = AccountService(session)
    accounts = await account_service.list(owner_ctx, include_archived=True)
    second = await account_service.create(
        owner_ctx,
        name="钱包",
        account_type=AccountType.CASH,
        currency="CNY",
    )
    await TransferService(session).create(
        owner_ctx,
        from_account_id=accounts[0].id,
        to_account_id=second.id,
        amount=Decimal("10"),
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
    )
    await session.commit()

    stats = await MemberStatsService(session).stats(owner_ctx)
    by_user = {item.user_id: item for item in stats}
    assert by_user[str(owner_ctx.actor_user_id)].expense_total == Decimal("100")
    assert by_user[str(owner_ctx.actor_user_id)].transaction_count == 1
    assert by_user[str(member_ctx.actor_user_id)].expense_total == Decimal("250")
    assert by_user[str(member_ctx.actor_user_id)].transaction_count == 2
    # Transfers excluded: no member has a 260 / 10 amount from the transfer.
    total = sum(item.expense_total for item in stats)
    assert total == Decimal("350")


async def test_entry_update_reassigns_payer(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    entry = await _record(session, owner_ctx, amount="100")
    assert entry.paid_by_user_id == owner_ctx.actor_user_id
    result = await LedgerService(
        session,
        commit_changes=False,
        paid_by_user_id=member_ctx.actor_user_id,
    ).execute(
        owner_ctx,
        ParsedCommand(action=Action.UPDATE_ENTRY, entry_ref=entry.short_id, note="已改"),
        source_type="web",
    )
    await session.commit()
    assert "已修改" in result.message
    refreshed = await session.get(LedgerEntry, entry.id)
    assert refreshed.paid_by_user_id == member_ctx.actor_user_id


async def test_created_message_includes_payer_when_different(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    result = await LedgerService(session, commit_changes=False).execute(
        owner_ctx,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("120"),
            direction=Direction.EXPENSE,
            category="餐饮",
            note="买菜",
            occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
            payer_reference="B",
        ),
    )
    await session.commit()
    assert "付款：B" in result.message
    assert result.message.startswith("已记录")

    # Default payer (actor) keeps the legacy reply shape — no payer suffix.
    plain = await LedgerService(session, commit_changes=False).execute(
        owner_ctx,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("50"),
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
        ),
    )
    await session.commit()
    assert "付款：" not in plain.message


async def test_feishu_list_pending_shows_household_recurring(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    account = (await AccountService(session).list(owner_ctx))[0]
    rule = await RecurringService(
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
        next_occurrence=date(2026, 9, 1),
        account_id=account.id,
    )
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    store = PendingCommandStore(factory, _settings())
    pending = await store.create_recurring_pending(
        session=session,
        context=owner_ctx,
        user_open_id="ou_owner",
        rule=rule,
        occurrence_date=date(2026, 9, 1),
        now=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    await session.commit()
    # Member B (not the creator) sees the shared recurring pending in their list.
    message, _ = await store.list_pending(
        user_open_id="ou_member",
        reply_to_message_id="om_list",
        event_id=None,
    )
    assert format_confirmation_ref(pending.confirmation_code) in message


async def test_recurring_payer_frozen_and_cross_member_confirm(
    session: AsyncSession,
) -> None:
    """Case G: shared recurring rule paid_by=B; A confirms; entry keeps paid_by=B."""
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    account = (await AccountService(session).list(owner_ctx))[0]
    rule = await RecurringService(
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
        next_occurrence=date(2026, 9, 1),
        account_id=account.id,
        paid_by_user_id=member_ctx.actor_user_id,
    )
    assert rule.paid_by_user_id == member_ctx.actor_user_id
    await session.commit()

    # The recurring pending freezes the rule's payer.
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    store = PendingCommandStore(factory, _settings())
    pending = await store.create_recurring_pending(
        session=session,
        context=owner_ctx,
        user_open_id="ou_owner",
        rule=rule,
        occurrence_date=date(2026, 9, 1),
        now=datetime(2026, 9, 1, 0, tzinfo=UTC),
    )
    assert pending.paid_by_user_id == member_ctx.actor_user_id
    code = pending.confirmation_code
    await session.commit()

    # A (owner) confirms a pending for the shared rule; payer stays B.
    message, _ = await store.confirm_and_execute(
        user_open_id="ou_owner",
        confirmation_code=code,
        reply_to_message_id="om_confirm",
        confirm_event_id=None,
        exchange_rates=None,
        now=datetime(2026, 9, 1, 0, 30, tzinfo=UTC),
    )
    assert "已记录" in message
    entry = (
        await session.execute(
            select(LedgerEntry).where(LedgerEntry.ledger_id == owner_ctx.ledger_id)
        )
    ).scalars().all()[-1]
    assert entry.created_by_user_id == owner_ctx.actor_user_id
    assert entry.paid_by_user_id == member_ctx.actor_user_id


async def test_cross_member_confirm_recurring_pending_by_code(
    session: AsyncSession,
) -> None:
    """A household member other than the pending creator can confirm by code."""
    owner_ctx, member_ctx, _, _ = await _household(session, "A", "B")
    account = (await AccountService(session).list(owner_ctx))[0]
    rule = await RecurringService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).create(
        owner_ctx,
        transaction_type=Direction.EXPENSE,
        amount=Decimal("200"),
        currency=None,
        category="餐饮",
        description="聚餐",
        frequency=RecurringFrequency.MONTHLY,
        interval=1,
        next_occurrence=date(2026, 9, 1),
        account_id=account.id,
        paid_by_user_id=member_ctx.actor_user_id,
    )
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    store = PendingCommandStore(factory, _settings())
    pending = await store.create_recurring_pending(
        session=session,
        context=owner_ctx,
        user_open_id="ou_owner",
        rule=rule,
        occurrence_date=date(2026, 9, 1),
        now=datetime(2026, 9, 1, 0, tzinfo=UTC),
    )
    code = pending.confirmation_code
    await session.commit()

    # B finds the pending by code even though the pending's creator is A.
    found = await store.get_by_code("ou_member", code)
    assert found is not None
    assert found.id == pending.id
    message, _ = await store.confirm_and_execute(
        user_open_id="ou_member",
        confirmation_code=code,
        reply_to_message_id="om_confirm_b",
        confirm_event_id=None,
        exchange_rates=None,
        now=datetime(2026, 9, 1, 0, 30, tzinfo=UTC),
    )
    assert "已记录" in message
    entry = (
        await session.execute(
            select(LedgerEntry).where(LedgerEntry.ledger_id == owner_ctx.ledger_id)
        )
    ).scalars().all()[-1]
    assert entry.created_by_user_id == member_ctx.actor_user_id
    assert entry.paid_by_user_id == member_ctx.actor_user_id
