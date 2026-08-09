from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import CategoryBudget, LedgerEntry
from lark_ledger.schemas import Action, Direction, ParsedCommand
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerAccessDeniedError, LedgerService
from lark_ledger.services.ledger_management import (
    LedgerManagementService,
    LedgerNameConflictError,
    LedgerNotFoundError,
)


@pytest.mark.asyncio
async def test_personal_ledgers_create_select_default_rename_and_isolate(
    session: AsyncSession,
) -> None:
    identities = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
    initial = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_multi"
    )
    manager = LedgerManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    travel = await manager.create(initial.actor_user_id, " 旅行 ")
    work = await manager.create(initial.actor_user_id, "工作")
    await manager.rename(initial.actor_user_id, work.id, "项目")

    with pytest.raises(LedgerNameConflictError):
        await manager.create(initial.actor_user_id, "旅-行")

    assert initial.channel_identity_id is not None
    await manager.select_for_channel(
        initial.actor_user_id, initial.channel_identity_id, travel.id
    )
    await manager.set_default(initial.actor_user_id, work.id)
    await session.commit()

    current = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_multi"
    )
    assert current.ledger_id == travel.id
    assert (await manager.get_default(initial.actor_user_id)).id == work.id
    listed = await manager.list_owned(initial.actor_user_id)
    assert listed[0].name == "项目"
    assert {ledger.name for ledger in listed} == {"项目", "我的账本", "旅行"}

    first_context = RequestContext(
        actor_user_id=initial.actor_user_id,
        ledger_id=initial.ledger_id,
        source_channel="feishu",
        external_subject_id="ou_multi",
    )
    travel_context = RequestContext(
        actor_user_id=initial.actor_user_id,
        ledger_id=travel.id,
        source_channel="feishu",
        external_subject_id="ou_multi",
    )
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
    )
    for context in (first_context, travel_context):
        await LedgerService(session, short_id_factory=lambda: "A83F2").execute(context, command)
        await LedgerService(session).execute(
            context,
            ParsedCommand(action=Action.SET_BUDGET, category="餐饮", amount=Decimal("1000")),
        )

    entries = (await session.scalars(select(LedgerEntry).order_by(LedgerEntry.ledger_id))).all()
    budgets = (await session.scalars(select(CategoryBudget))).all()
    assert len(entries) == 2
    assert {entry.ledger_id for entry in entries} == {initial.ledger_id, travel.id}
    assert {entry.short_id for entry in entries} == {"A83F2"}
    assert len(budgets) == 2
    assert {budget.category for budget in budgets} == {"餐饮"}


@pytest.mark.asyncio
async def test_personal_ledger_ownership_is_enforced(session: AsyncSession) -> None:
    identities = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
    owner = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_owner"
    )
    outsider = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_outsider"
    )
    manager = LedgerManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    with pytest.raises(LedgerNotFoundError):
        await manager.get_owned(outsider.actor_user_id, owner.ledger_id)
    assert outsider.channel_identity_id is not None
    with pytest.raises(LedgerNotFoundError):
        await manager.select_for_channel(
            outsider.actor_user_id, outsider.channel_identity_id, owner.ledger_id
        )
    with pytest.raises(LedgerAccessDeniedError):
        await LedgerService(session).execute(
            RequestContext(
                actor_user_id=outsider.actor_user_id,
                ledger_id=owner.ledger_id,
                source_channel="web",
            ),
            ParsedCommand(action=Action.LIST_ENTRIES),
        )
