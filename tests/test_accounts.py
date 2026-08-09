from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import AccountStatus, AccountType, LedgerEntry
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.accounts import AccountConflictError, AccountNotFoundError, AccountService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.ledger_management import LedgerManagementService


async def test_account_lifecycle_is_ledger_scoped(session: AsyncSession) -> None:
    identity = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
    context = await identity.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_accounts"
    )
    service = AccountService(session)
    defaults = await service.list(context)
    assert len(defaults) == 1
    assert defaults[0].name == "默认账户"
    assert defaults[0].is_default is True

    bank = await service.create(
        context,
        name="招商银行",
        account_type=AccountType.ASSET,
        subtype="bank_card",
        provider="CMB",
        opening_balance=Decimal("1200.50"),
    )
    assert bank.ledger_id == context.ledger_id
    await service.rename(context, bank.id, "招商银行卡")
    await service.set_default(context, bank.id)
    assert bank.is_default is True
    await service.archive(context, defaults[0].id)
    assert defaults[0].status == AccountStatus.ARCHIVED.value

    other_ledger = await LedgerManagementService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).create(context.actor_user_id, "旅行")
    other_context = context.__class__(
        actor_user_id=context.actor_user_id,
        ledger_id=other_ledger.id,
        source_channel="test",
    )
    with pytest.raises(AccountNotFoundError):
        await service.get(other_context, bank.id)
    with pytest.raises(AccountConflictError):
        await service.archive(context, bank.id)


async def test_legacy_and_explicit_entry_writes_bind_validated_accounts(
    session: AsyncSession,
) -> None:
    context = await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_entry_account")
    accounts = AccountService(session)
    default = await accounts.get_default(context)
    wallet = await accounts.create(
        context,
        name="支付宝",
        account_type=AccountType.ASSET,
        subtype="wallet",
        provider="alipay",
    )
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("12"),
        direction="expense",
        category="餐饮",
        occurred_at="2026-08-09T12:00:00+08:00",
    )
    await LedgerService(session).execute(context, command)
    await LedgerService(session, account_id=wallet.id).execute(context, command)
    rows = list(
        (
            await session.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.ledger_id == context.ledger_id)
                .order_by(LedgerEntry.created_at, LedgerEntry.id)
            )
        ).all()
    )
    assert {row.account_id for row in rows} == {default.id, wallet.id}

    other = await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_other_account")
    foreign = await accounts.get_default(other)
    with pytest.raises(AccountNotFoundError):
        await LedgerService(session, account_id=foreign.id).execute(context, command)
