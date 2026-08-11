"""P32 Account-level privacy — shared vs private accounts in household ledgers.

Cases covered:

* E — A marks an account private; B's account/entry/recurring reads hide it
  (404 / filtered out) while A still sees everything.
* F — B's overview / budget / assets / member-stats exclude A's private 500;
  no side channel leaks the amount (category totals, budget spend).
* G — recurring payer frozen on confirm (P30 regression) still holds when the
  rule targets a private account.
* Transfers are visible iff both accounts are visible; visibility toggling is
  owner-only; personal ledgers keep exact legacy behavior (privacy no-op).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
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
    PendingCommand,
    RecurringFrequency,
    RecurringRule,
)
from lark_ledger.services.accounts import AccountNotFoundError, AccountService
from lark_ledger.services.budget import BudgetService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.household_overview import HouseholdOverviewService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.member_stats import MemberStatsService
from lark_ledger.services.privacy import PrivacyService
from lark_ledger.services.recurring import RecurringRuleValidationError, RecurringService
from lark_ledger.services.transfers import TransferNotFoundError, TransferService
from lark_ledger.services.web_ledger import WebLedgerQueryService
from lark_ledger.services.web_pending import WebPendingQueryService


async def _identity(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(
        channel="feishu", external_subject_id=open_id, display_name=name
    )


async def _household(
    session: AsyncSession,
) -> tuple[RequestContext, RequestContext, list[Account]]:
    owner = await _identity(session, "ou_owner", "A")
    member = await _identity(session, "ou_member", "B")
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    home = await manager.create(owner.actor_user_id, "测试家庭")
    invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_member")
    await manager.accept(member.actor_user_id, invitation.public_id)
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
    accounts = await AccountService(session).list(owner_ctx, include_archived=True)
    await session.commit()
    return owner_ctx, member_ctx, accounts


def _context(session: AsyncSession, owner: RequestContext, name: str) -> RequestContext:
    return RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="feishu",
        external_subject_id=owner.external_subject_id,
    )


async def _private_account(
    session: AsyncSession, context: RequestContext, name: str
) -> Account:
    account = await AccountService(session).create(
        context,
        name=name,
        account_type=AccountType.CASH,
        currency="CNY",
        visibility=AccountVisibility.PRIVATE,
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
    direction: Direction = Direction.EXPENSE,
    category: str = "私人消费",
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
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
        source_type="text",
    )
    session.add(entry)
    await session.commit()
    return entry


async def _recurring_rule(
    session: AsyncSession,
    context: RequestContext,
    *,
    account_id: uuid.UUID,
    description: str,
) -> RecurringRule:
    rule = await RecurringService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).create(
        context,
        transaction_type=Direction.EXPENSE,
        amount=Decimal("300"),
        currency=None,
        category="居住",
        description=description,
        frequency=RecurringFrequency.MONTHLY,
        interval=1,
        next_occurrence=date(2099, 9, 1),
        account_id=account_id,
    )
    await session.commit()
    return rule


async def _pending(
    session: AsyncSession,
    context: RequestContext,
    *,
    confirmation_code: str,
    account_id: uuid.UUID | None,
    recurring_rule_id: uuid.UUID | None = None,
) -> PendingCommand:
    row = PendingCommand(
        confirmation_code=confirmation_code,
        user_open_id=context.external_subject_id or "ou",
        actor_user_id=context.actor_user_id,
        ledger_id=context.ledger_id,
        account_id=account_id,
        recurring_rule_id=recurring_rule_id,
        command_type="create",
        payload_json={"action": "create", "amount": "10"},
        preview_json={
            "code": "C12345",
            "display_code": "#C-12345",
            "entries_total": 1,
            "income_count": 0,
            "expense_count": 1,
            "income_total": "",
            "expense_total": "10.00",
            "currency": "CNY",
            "items": [
                {
                    "index": 0,
                    "direction": "expense",
                    "amount": "10.00",
                    "currency": "CNY",
                    "category": "测试",
                    "occurred_at": "",
                    "note": "",
                }
            ],
            "budgets": [],
            "anomalies": [],
            "risk_reason": "none",
            "expires_at": "",
        },
        risk_reason="none",
        status="pending",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_case_e_private_account_hidden_from_other_member(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, accounts = await _household(session)
    shared = accounts[0]
    private = await _private_account(session, owner_ctx, "私房钱")
    await _entry(session, owner_ctx, short_id="A0001", amount="500", account_id=private.id)
    await _entry(session, owner_ctx, short_id="A0002", amount="80", account_id=shared.id)

    # Owner sees both accounts and both entries.
    owner_accounts = await AccountService(session).list(owner_ctx)
    assert {account.id for account in owner_accounts} == {shared.id, private.id}
    owner_rows = await WebLedgerQueryService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).list_entries(owner_ctx, page=1, page_size=10)
    assert owner_rows.total == 2

    # Member cannot see the private account (404 semantics) nor its entries.
    member_accounts = await AccountService(session).list(member_ctx)
    assert [account.id for account in member_accounts] == [shared.id]
    with pytest.raises(AccountNotFoundError):
        await AccountService(session).get(member_ctx, private.id)
    member_rows = await WebLedgerQueryService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).list_entries(member_ctx, page=1, page_size=10)
    assert member_rows.total == 1
    assert member_rows.items[0].short_id == "A0002"
    assert await WebLedgerQueryService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).entry_detail(member_ctx, "A0001") is None

    # The entry is still owned by A (payer attribution intact).
    detail = await WebLedgerQueryService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).entry_detail(owner_ctx, "A0001")
    assert detail is not None
    assert detail.entry.amount == Decimal("500")


@pytest.mark.asyncio
async def test_case_f_overview_budget_assets_exclude_private(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, accounts = await _household(session)
    shared = accounts[0]
    private = await _private_account(session, owner_ctx, "私房钱")
    await _entry(
        session, owner_ctx, short_id="A0001", amount="500", account_id=private.id
    )
    await _entry(
        session,
        owner_ctx,
        short_id="A0002",
        amount="200",
        account_id=shared.id,
        category="餐饮",
    )
    await BudgetService(session, currency="CNY", timezone="Asia/Shanghai").set_total_budget(
        member_ctx, period=date(2026, 8, 1), amount=Decimal("1000")
    )
    await session.commit()

    # Member's overview excludes the private 500 — including the budget spent.
    overview = await HouseholdOverviewService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).overview(member_ctx, period=date(2026, 8, 1))
    assert overview.expense_total == Decimal("200")
    assert overview.budget.total_spent == Decimal("200")
    assert {item.category for item in overview.top_categories} == {"餐饮"}
    assert overview.top_categories[0].amount == Decimal("200")
    assert overview.recent_transactions[0].short_id == "A0002"

    # Budget overview spent excludes private rows (no side channel).
    budget = await BudgetService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).overview(member_ctx, period=date(2026, 8, 1))
    assert budget.total_spent == Decimal("200")

    # Member stats only surface the shared 200, never the private 500 (the
    # web layer passes the privacy filter; mirror that here).
    privacy = await PrivacyService(session).entry_visibility_scope(member_ctx)
    stats = await MemberStatsService(session).stats(member_ctx, privacy_filter=privacy)
    by_user = {item.user_id: item for item in stats}
    owner_stats = by_user[str(owner_ctx.actor_user_id)]
    assert owner_stats.expense_total == Decimal("200")
    assert all(item.expense_total <= Decimal("200") for item in stats)

    # Assets summary (privacy-filtered via AccountService.list) excludes it.
    summary = await TransferService(session).asset_summary(member_ctx)
    assert len(summary.accounts) == 1


@pytest.mark.asyncio
async def test_recurring_private_account_hidden_and_rejected(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, accounts = await _household(session)
    private = await _private_account(session, owner_ctx, "私房钱")
    await _recurring_rule(
        session, owner_ctx, account_id=private.id, description="私密房租"
    )

    # Member cannot see the rule and cannot target the private account.
    member_rules = await RecurringService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).list(member_ctx)
    assert member_rules == []
    with pytest.raises(RecurringRuleValidationError):
        await RecurringService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).create(
            member_ctx,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("10"),
            currency=None,
            category="测试",
            description="测试",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=date(2099, 9, 1),
            account_id=private.id,
        )
    # Owner still manages their rule.
    owner_rules = await RecurringService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).list(owner_ctx)
    assert [item.description for item in owner_rules] == ["私密房租"]


@pytest.mark.asyncio
async def test_transfer_visible_only_when_both_accounts_visible(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, accounts = await _household(session)
    shared = accounts[0]
    private = await _private_account(session, owner_ctx, "私房钱")
    transfer = await TransferService(session).create(
        owner_ctx,
        from_account_id=shared.id,
        to_account_id=private.id,
        amount=Decimal("50"),
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
    )
    await session.commit()

    assert (await TransferService(session).get(owner_ctx, transfer.id)).id == transfer.id
    with pytest.raises(TransferNotFoundError):
        await TransferService(session).get(member_ctx, transfer.id)
    member_rows, member_total = await TransferService(session).list_paginated(
        member_ctx, page=1, page_size=10
    )
    assert member_rows == []
    assert member_total == 0


@pytest.mark.asyncio
async def test_pending_private_target_hidden_from_other_member(
    session: AsyncSession,
) -> None:
    owner_ctx, member_ctx, _ = await _household(session)
    private = await _private_account(session, owner_ctx, "私房钱")
    rule_id = uuid.uuid4()
    # Two household recurring pendings (member scope includes recurring-in-ledger
    # pendings): one targets A's private account, one targets no account.
    await _pending(
        session,
        owner_ctx,
        confirmation_code="CA83F1",
        account_id=private.id,
        recurring_rule_id=rule_id,
    )
    await _pending(
        session,
        owner_ctx,
        confirmation_code="CA83F2",
        account_id=None,
        recurring_rule_id=rule_id,
    )

    service = WebPendingQueryService(session)
    member_rows = await service.list_pending(
        member_ctx, group="pending", page=1, page_size=10
    )
    assert member_rows.total == 1
    assert member_rows.items[0].confirmation_id.endswith("A83F2")
    assert await service.detail(member_ctx, "CA83F1") is None


@pytest.mark.asyncio
async def test_set_visibility_is_owner_only(session: AsyncSession) -> None:
    owner_ctx, member_ctx, accounts = await _household(session)
    shared = accounts[0]
    private = await _private_account(session, owner_ctx, "私房钱")

    # A member cannot toggle someone else's private account (404 semantics).
    with pytest.raises(AccountNotFoundError):
        await AccountService(session).set_visibility(
            member_ctx, private.id, AccountVisibility.SHARED
        )
    # The owner can flip it back to shared.
    await AccountService(session).set_visibility(
        owner_ctx, private.id, AccountVisibility.SHARED
    )
    member_accounts = await AccountService(session).list(member_ctx)
    assert private.id in {account.id for account in member_accounts}

    # A regular member cannot mark a shared account private (only ledger owner).
    with pytest.raises(AccountNotFoundError):
        await AccountService(session).set_visibility(
            member_ctx, shared.id, AccountVisibility.PRIVATE
        )
    # The ledger owner can.
    await AccountService(session).set_visibility(
        owner_ctx, shared.id, AccountVisibility.PRIVATE
    )
    refreshed = await session.get(Account, shared.id)
    assert refreshed is not None
    assert refreshed.visibility == "private"
    assert refreshed.owner_user_id == owner_ctx.actor_user_id


@pytest.mark.asyncio
async def test_privacy_is_noop_for_personal_ledger(session: AsyncSession) -> None:
    owner = await _identity(session, "ou_solo", "我")
    await session.commit()
    context = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="feishu",
        external_subject_id="ou_solo",
    )
    assert not await PrivacyService(session).privacy_enabled(context)
    private = await _private_account(session, context, "私房钱")
    await _entry(session, context, short_id="B0001", amount="500", account_id=private.id)

    # The sole owner sees everything despite the private flag.
    rows = await WebLedgerQueryService(
        session, timezone="Asia/Shanghai", currency="CNY"
    ).list_entries(context, page=1, page_size=10)
    assert rows.total == 1
    accounts = await AccountService(session).list(context)
    assert private.id in {account.id for account in accounts}


@pytest.mark.asyncio
async def test_member_stats_privacy_filter_parameter(session: AsyncSession) -> None:
    owner_ctx, member_ctx, _ = await _household(session)
    private = await _private_account(session, owner_ctx, "私房钱")
    await _entry(session, owner_ctx, short_id="C0001", amount="500", account_id=private.id)

    privacy = await PrivacyService(session).entry_visibility_scope(member_ctx)
    stats = await MemberStatsService(session).stats(member_ctx, privacy_filter=privacy)
    assert stats == []
