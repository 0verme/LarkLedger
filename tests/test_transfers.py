from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import (
    AccountType,
    CategoryBudget,
    Direction,
    LedgerEntry,
    PendingCommand,
    Transfer,
    TransferRevision,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.accounts import AccountNotFoundError, AccountService
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.services.risk import RiskAssessment, RiskDecision, RiskReason
from lark_ledger.services.transfers import (
    TransferConflictError,
    TransferNotFoundError,
    TransferService,
)
from lark_ledger.services.web_analytics import WebAnalyticsQueryService


async def _setup(session: AsyncSession, subject: str = "ou_transfer"):
    context = await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)
    accounts = AccountService(session)
    default = await accounts.get_default(context)
    default.name = "招商银行"
    default.normalized_name = "招商银行"
    wallet = await accounts.create(
        context,
        name="支付宝",
        account_type=AccountType.ASSET,
        opening_balance=Decimal("100"),
    )
    liability = await accounts.create(
        context,
        name="信用卡",
        account_type=AccountType.LIABILITY,
        opening_balance=Decimal("200"),
    )
    await session.flush()
    return context, default, wallet, liability


def _entry(context, account_id, direction: Direction, amount: str, short_id: str) -> LedgerEntry:
    return LedgerEntry(
        user_open_id="ou_transfer",
        ledger_id=context.ledger_id,
        account_id=account_id,
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=direction,
        category="测试",
        note="",
        occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
        source_type="text",
    )


async def test_income_expense_transfer_revision_reversal_and_assets(
    session: AsyncSession,
) -> None:
    context, bank, wallet, liability = await _setup(session)
    session.add_all(
        [
            _entry(context, bank.id, Direction.INCOME, "500", "AA001"),
            _entry(context, bank.id, Direction.EXPENSE, "80", "AA002"),
            _entry(context, liability.id, Direction.EXPENSE, "30", "AA003"),
        ]
    )
    session.add(
        CategoryBudget(
            user_open_id="ou_transfer",
            ledger_id=context.ledger_id,
            category="测试",
            amount=Decimal("1000"),
        )
    )
    service = TransferService(session)
    transfer = await service.create(
        context,
        from_account_id=bank.id,
        to_account_id=wallet.id,
        amount=Decimal("120"),
        occurred_at=datetime(2026, 8, 9, 8, tzinfo=UTC),
    )

    assert (await service.account_balance(context, bank.id)).current_balance == Decimal("300")
    assert (await service.account_balance(context, wallet.id)).current_balance == Decimal("220")
    assert (await service.account_balance(context, liability.id)).current_balance == Decimal("230")

    await service.revise(context, transfer.id, amount=Decimal("100"))
    assert (await service.account_balance(context, bank.id)).current_balance == Decimal("320")
    assert (await service.account_balance(context, wallet.id)).current_balance == Decimal("200")
    await service.reverse(context, transfer.id)
    assert (await service.account_balance(context, bank.id)).current_balance == Decimal("420")
    assert (await service.account_balance(context, wallet.id)).current_balance == Decimal("100")
    with pytest.raises(TransferConflictError, match="已经撤销"):
        await service.reverse(context, transfer.id)
    assert await session.scalar(select(func.count()).select_from(TransferRevision)) == 2

    summary = await service.asset_summary(context)
    assert summary.total_assets == Decimal("520")
    assert summary.total_liabilities == Decimal("230")
    assert summary.net_assets == Decimal("290")
    analytics = WebAnalyticsQueryService(session, timezone="Asia/Shanghai", currency="CNY")
    stats, _, categories, _ = await analytics.analytics(
        context, start_date=date(2026, 8, 9), end_date=date(2026, 8, 9)
    )
    budget = await analytics.budgets(context, now=datetime(2026, 8, 9, 12, tzinfo=UTC))
    assert (stats.income, stats.expense, stats.entry_count) == (
        Decimal("500"),
        Decimal("110"),
        3,
    )
    assert categories[0].amount == Decimal("110")
    assert budget.total_spent == Decimal("110")


async def test_transfer_is_ledger_scoped_distinct_atomic_and_archived_balance(
    session: AsyncSession,
) -> None:
    context, bank, wallet, _ = await _setup(session)
    service = TransferService(session)
    with pytest.raises(TransferConflictError, match="不能相同"):
        await service.create(
            context,
            from_account_id=bank.id,
            to_account_id=bank.id,
            amount=Decimal("1"),
            occurred_at=datetime.now(UTC),
        )
    assert await session.scalar(select(func.count()).select_from(Transfer)) == 0

    other_context, other_default, _, _ = await _setup(session, "ou_other")
    with pytest.raises(AccountNotFoundError):
        await service.create(
            context,
            from_account_id=bank.id,
            to_account_id=other_default.id,
            amount=Decimal("1"),
            occurred_at=datetime.now(UTC),
        )
    assert await session.scalar(select(func.count()).select_from(Transfer)) == 0

    transfer = await service.create(
        context,
        from_account_id=bank.id,
        to_account_id=wallet.id,
        amount=Decimal("10"),
        occurred_at=datetime.now(UTC),
    )
    with pytest.raises(TransferNotFoundError):
        await service.get(other_context, transfer.id)
    await AccountService(session).archive(context, wallet.id)
    archived = await service.account_balance(context, wallet.id)
    assert archived.archived is True
    assert archived.current_balance == Decimal("110")


async def test_liability_transfer_direction(session: AsyncSession) -> None:
    context, bank, _, liability = await _setup(session)
    service = TransferService(session)
    await service.create(
        context,
        from_account_id=bank.id,
        to_account_id=liability.id,
        amount=Decimal("50"),
        occurred_at=datetime.now(UTC),
    )
    assert (await service.account_balance(context, bank.id)).current_balance == Decimal("-50")
    # Paying a liability reduces positive debt.
    assert (await service.account_balance(context, liability.id)).current_balance == Decimal("150")


async def test_bare_account_and_transfer_ids_never_cross_ledger(session: AsyncSession) -> None:
    context, bank, wallet, _ = await _setup(session)
    other_context, _, _, _ = await _setup(session, "ou_outsider")
    transfer = await TransferService(session).create(
        context,
        from_account_id=bank.id,
        to_account_id=wallet.id,
        amount=Decimal("5"),
        occurred_at=datetime.now(UTC),
    )
    with pytest.raises(AccountNotFoundError):
        await TransferService(session).account_balance(other_context, bank.id)
    with pytest.raises(TransferNotFoundError):
        await TransferService(session).get(other_context, transfer.id)
    with pytest.raises(TransferNotFoundError):
        await TransferService(session).get(context, uuid.uuid4())


async def test_pending_transfer_keeps_frozen_ledger_accounts_and_transfer_id() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        from lark_ledger.models import Base

        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, pending_expires_seconds=600)
    store = PendingCommandStore(factory, settings)
    now = datetime.now(UTC)
    async with factory() as db:
        context, bank, wallet, _ = await _setup(db, "ou_pending_transfer")
        pending = await store.create_pending(
            session=db,
            event_id="evt-transfer-pending",
            message_id="om-transfer-pending",
            source_fingerprint=None,
            user_open_id="ou_pending_transfer",
            command=ParsedCommand(
                action=Action.TRANSFER,
                amount=Decimal("25"),
                occurred_at=now,
                from_account_hint="招商银行",
                to_account_hint="支付宝",
            ),
            source_type="text",
            risk=RiskAssessment(
                decision=RiskDecision.PENDING,
                reason=RiskReason.TRANSFER,
            ),
            now=now,
            context=context,
        )
        frozen = (
            pending.ledger_id,
            pending.from_account_id,
            pending.to_account_id,
            pending.transfer_id,
        )
        assert frozen == (context.ledger_id, bank.id, wallet.id, pending.transfer_id)
        new_ledger = await ClientApplicationService(
            db, currency="CNY", timezone="Asia/Shanghai"
        ).create_personal_ledger(context, "切换后的账本")
        assert context.channel_identity_id is not None
        await ClientApplicationService(
            db, currency="CNY", timezone="Asia/Shanghai"
        ).select_channel_ledger(context, new_ledger.id)
        await db.commit()

    message, _ = await store.confirm_and_execute(
        user_open_id="ou_pending_transfer",
        confirmation_code=pending.confirmation_code,
        reply_to_message_id="om-confirm",
        confirm_event_id=None,
        exchange_rates=None,
        now=now,
    )
    assert "转账已创建" in message
    async with factory() as db:
        row = await db.get(Transfer, pending.transfer_id)
        assert row is not None
        assert (row.ledger_id, row.from_account_id, row.to_account_id, row.id) == frozen
        stored = await db.get(PendingCommand, pending.id)
        assert stored is not None and stored.status == "executed"
    await engine.dispose()
