"""P39 — Unified AI Entry on real PostgreSQL.

The core AI execution path must be verified against PostgreSQL, not just an
in-memory mock: create / update / delete / restore / transfer / idempotency /
pending confirmation / ledger isolation / private isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.models import (
    AccountType,
    AccountVisibility,
    Direction,
    LedgerEntry,
    PendingCommand,
    PendingStatus,
)
from lark_ledger.schemas import Action, AIEntryStatus
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.ai_entry import AIEntryRequest, UnifiedAIEntryService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from tests.test_ai_entry import (
    StubInterpreter,
    _command,
)

pytestmark = pytest.mark.postgres


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        lark_app_id="app_id",
        lark_app_secret="app_secret",
        currency="CNY",
        timezone="Asia/Shanghai",
        pending_enabled=True,
    )


def _request(context: RequestContext, text: str, request_id: str) -> AIEntryRequest:
    return AIEntryRequest(
        context=context,
        text=text,
        request_id=request_id,
        source_message_ref=request_id,
    )


async def _identity(
    factory: async_sessionmaker[AsyncSession], open_id: str, display_name: str = ""
) -> RequestContext:
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id=open_id, display_name=display_name
        )
        await session.commit()
        return context


async def _submit_web(
    factory: async_sessionmaker[AsyncSession],
    interpreter: StubInterpreter,
    context: RequestContext,
    text: str,
    request_id: str,
    *,
    pending_enabled: bool = True,
):
    settings = _settings()
    if not pending_enabled:
        settings = settings.model_copy(update={"pending_enabled": False})
    service = UnifiedAIEntryService(settings, factory, interpreter=interpreter)
    web_context = RequestContext(
        actor_user_id=context.actor_user_id,
        ledger_id=context.ledger_id,
        source_channel="web",
        external_subject_id=context.external_subject_id,
        actor_kind="user",
    )
    async with factory() as session:
        return await service.submit(
            session=session,
            request=_request(web_context, text, request_id),
            commit_changes=True,
        )


async def _count(factory: async_sessionmaker[AsyncSession], ledger_id=None) -> int:
    async with factory() as session:
        query = select(func.count()).select_from(LedgerEntry)
        if ledger_id is not None:
            query = query.where(LedgerEntry.ledger_id == ledger_id)
        return int(await session.scalar(query) or 0)


# ---------------------------------------------------------------------------
# Core execution on PostgreSQL
# ---------------------------------------------------------------------------


async def test_pg_create_update_delete_restore(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(postgres_session_factory, "ou_pg_ai01")
    create = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    )
    outcome = await _submit_web(
        postgres_session_factory, StubInterpreter(create), ctx, "午饭28", "pg:ai:01"
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.resource_id is not None
    async with postgres_session_factory() as session:
        row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "pg:ai:01")
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        assert row.amount == Decimal("28.00")
        assert row.direction is Direction.EXPENSE
        assert row.category == "餐饮"
        short_id = row.short_id

    # update_last → 30
    outcome = await _submit_web(
        postgres_session_factory,
        StubInterpreter(_command(Action.UPDATE_LAST, amount="30.00")),
        ctx,
        "改成30",
        "pg:ai:02",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    async with postgres_session_factory() as session:
        row = await session.scalar(select(LedgerEntry).where(LedgerEntry.short_id == short_id))
        assert row.amount == Decimal("30.00")

    # delete then restore via short id
    outcome = await _submit_web(
        postgres_session_factory,
        StubInterpreter(_command(Action.DELETE_ENTRY, entry_ref=short_id)),
        ctx,
        f"删除 #{short_id}",
        "pg:ai:03",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    outcome = await _submit_web(
        postgres_session_factory,
        StubInterpreter(_command(Action.RESTORE_ENTRY, entry_ref=short_id)),
        ctx,
        f"恢复 #{short_id}",
        "pg:ai:04",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    async with postgres_session_factory() as session:
        row = await session.scalar(select(LedgerEntry).where(LedgerEntry.short_id == short_id))
        assert row.deleted_at is None


async def test_pg_transfer_pending_then_confirm(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(postgres_session_factory, "ou_pg_ai02")
    async with postgres_session_factory() as session:
        await AccountService(session).create(
            ctx, name="招行", account_type=AccountType.ASSET, currency="CNY"
        )
        await AccountService(session).create(
            ctx, name="支付宝", account_type=AccountType.ASSET, currency="CNY"
        )
        await session.commit()
    transfer = _command(
        Action.TRANSFER,
        amount="1000.00",
        from_account_hint="招行",
        to_account_hint="支付宝",
    )
    outcome = await _submit_web(
        postgres_session_factory, StubInterpreter(transfer), ctx, "从招行转1000到支付宝", "pg:ai:t1"
    )
    assert outcome.status is AIEntryStatus.CONFIRMATION_REQUIRED
    assert outcome.pending_command_id is not None
    assert await _count(postgres_session_factory) == 0

    # Confirm through the pending store (exactly-once path).
    async with postgres_session_factory() as session:
        pending = await session.scalar(select(PendingCommand))
        assert pending is not None
        assert pending.status == PendingStatus.PENDING.value
        code = pending.confirmation_code
        ledger_id = pending.ledger_id
        actor_user_id = pending.actor_user_id

    from lark_ledger.services.pending import PendingCommandStore

    store = PendingCommandStore(postgres_session_factory, _settings())
    message, _rows = await store.confirm_and_execute(
        user_open_id="ou_pg_ai02",
        confirmation_code=code,
        reply_to_message_id="pg:ai:confirm",
        confirm_event_id=None,
        exchange_rates=None,
        now=datetime.now(UTC),
    )
    assert "转账已创建" in message
    # Confirming again is idempotent: terminal status message, no second row.
    message2, _rows2 = await store.confirm_and_execute(
        user_open_id="ou_pg_ai02",
        confirmation_code=code,
        reply_to_message_id="pg:ai:confirm2",
        confirm_event_id=None,
        exchange_rates=None,
        now=datetime.now(UTC),
    )
    assert "已确认并已入账，无需重复操作" in message2
    async with postgres_session_factory() as session:
        from lark_ledger.models import Transfer

        transfers = (await session.scalars(select(Transfer))).all()
        assert len(transfers) == 1
        assert transfers[0].ledger_id == ledger_id
        assert transfers[0].amount == Decimal("1000.00")
        pending = await session.get(PendingCommand, pending.id)
        assert pending.status == PendingStatus.EXECUTED.value
    assert actor_user_id == ctx.actor_user_id


async def test_pg_ledger_and_private_isolation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_pg_o", display_name="甲")
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_pg_m", display_name="乙")
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_pg_m")
        await manager.accept(member.actor_user_id, invitation.public_id)
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="web",
            external_subject_id="ou_pg_o",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="web",
            external_subject_id="ou_pg_m",
        )
        private = await AccountService(session).create(
            owner_ctx, name="私房钱", account_type=AccountType.CASH, currency="CNY"
        )
        await AccountService(session).set_visibility(
            owner_ctx, private.id, visibility=AccountVisibility.PRIVATE
        )
        await session.commit()

    # Member cannot use the owner's private account via natural language.
    outcome = await _submit_web(
        postgres_session_factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="100.00",
                direction=Direction.EXPENSE,
                category="其他",
                account_hint="私房钱",
            )
        ),
        member_ctx,
        "用私房钱100",
        "pg:ai:priv",
    )
    assert outcome.status is AIEntryStatus.ERROR
    assert "私房钱" not in outcome.message
    assert await _count(postgres_session_factory, home.ledger.id) == 0

    # Member cannot write into the owner's personal ledger via a guessed id.
    forged = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="web",
        external_subject_id="ou_pg_m",
    )
    outcome = await _submit_web(
        postgres_session_factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
        forged,
        "午饭28",
        "pg:ai:cross",
    )
    assert outcome.status is AIEntryStatus.ERROR
    assert await _count(postgres_session_factory, owner.ledger_id) == 0


async def test_pg_web_http_idempotency_via_application(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same Idempotency-Key through ClientIdempotencyService yields exactly
    one ledger row on PostgreSQL (P39 §23–§25)."""
    from lark_ledger.services.client_idempotency import ClientIdempotencyService

    ctx = await _identity(postgres_session_factory, "ou_pg_ai03")
    create = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    )
    service = UnifiedAIEntryService(
        _settings(), postgres_session_factory, interpreter=StubInterpreter(create)
    )
    web_context = RequestContext(
        actor_user_id=ctx.actor_user_id,
        ledger_id=ctx.ledger_id,
        source_channel="web",
        external_subject_id="ou_pg_ai03",
    )
    first: object | None = None
    replayed_first = False
    replayed_second = False
    async with postgres_session_factory() as session:

        async def apply(_record):
            return (
                await service.submit(
                    session=session,
                    request=_request(web_context, "午饭28", "pg:ai:idem"),
                    commit_changes=False,
                )
            ).model_dump(mode="json")

        first, replayed_first = await ClientIdempotencyService(session).execute(
            web_context,
            operation="web.ai.entry",
            key="pg-idem-key",
            payload={"text": "午饭28"},
            callback=apply,
            response_status=200,
        )
    async with postgres_session_factory() as session:
        second, replayed_second = await ClientIdempotencyService(session).execute(
            web_context,
            operation="web.ai.entry",
            key="pg-idem-key",
            payload={"text": "午饭28"},
            callback=apply,
            response_status=200,
        )
    assert first["status"] == "executed"
    assert replayed_first is False
    assert second["status"] == "executed"
    assert replayed_second is True
    assert await _count(postgres_session_factory, ctx.ledger_id) == 1
