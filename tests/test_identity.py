from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import ChannelIdentity, Ledger, LedgerEntry, User, UserStatus
from lark_ledger.schemas import Action, Direction, ParsedCommand
from lark_ledger.services.identity import IdentityDisabledError, IdentityService
from lark_ledger.services.ledger import LedgerAccessDeniedError, LedgerService


@pytest.mark.asyncio
async def test_feishu_identity_bootstraps_one_user_and_default_ledger(
    session: AsyncSession,
) -> None:
    service = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")

    first = await service.resolve_or_bootstrap(
        channel="Feishu", external_subject_id="ou_user", display_name="小飞"
    )
    second = await service.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_user", display_name="新名字"
    )

    assert second == first
    assert first.source_channel == "feishu"
    assert first.external_subject_id == "ou_user"
    assert await session.scalar(select(func.count()).select_from(User)) == 1
    assert await session.scalar(select(func.count()).select_from(ChannelIdentity)) == 1
    ledger = (await session.execute(select(Ledger))).scalar_one()
    assert ledger.id == first.ledger_id
    assert ledger.owner_user_id == first.actor_user_id
    assert ledger.is_default is True


@pytest.mark.asyncio
async def test_disabled_internal_user_is_rejected(session: AsyncSession) -> None:
    service = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
    context = await service.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_disabled"
    )
    user = await session.get(User, context.actor_user_id)
    assert user is not None
    user.status = UserStatus.DISABLED.value
    await session.flush()

    with pytest.raises(IdentityDisabledError):
        await service.resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_disabled"
        )


@pytest.mark.asyncio
async def test_ledger_service_persists_internal_ledger_scope(session: AsyncSession) -> None:
    context = await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_user")

    await LedgerService(session).execute(
        context,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("28"),
            direction=Direction.EXPENSE,
            category="餐饮",
            note="午饭",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
        ),
    )

    entry = (await session.execute(select(LedgerEntry))).scalar_one()
    assert entry.ledger_id == context.ledger_id
    assert entry.user_open_id == "ou_user"


@pytest.mark.asyncio
async def test_ledger_service_rejects_actor_from_another_ledger(
    session: AsyncSession,
) -> None:
    identities = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
    owner = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_owner"
    )
    outsider = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_outsider"
    )
    forged = RequestContext(
        actor_user_id=outsider.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="web",
        external_subject_id="ou_outsider",
    )

    with pytest.raises(LedgerAccessDeniedError):
        await LedgerService(session).execute(
            forged,
            ParsedCommand(action=Action.LIST_ENTRIES),
        )
