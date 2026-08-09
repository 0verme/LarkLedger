from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    ChannelIdentity,
    HouseholdInvitationStatus,
    HouseholdMember,
    HouseholdMemberStatus,
    LedgerEntry,
    LedgerKind,
)
from lark_ledger.schemas import Action, Direction, ParsedCommand
from lark_ledger.services.household_management import (
    HouseholdConflictError,
    HouseholdManagementService,
    HouseholdPermissionError,
)
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerAccessDeniedError, LedgerService
from lark_ledger.services.ledger_management import LedgerManagementService, LedgerNotFoundError


async def _identity(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(
        channel="feishu", external_subject_id=open_id, display_name=name
    )


@pytest.mark.asyncio
async def test_household_invitation_shared_ledger_and_private_ledger_boundaries(
    session: AsyncSession,
) -> None:
    owner = await _identity(session, "ou_house_owner", "所有者")
    member = await _identity(session, "ou_house_member", "成员")
    outsider = await _identity(session, "ou_house_outsider", "外部用户")
    manager = HouseholdManagementService(
        session, currency="CNY", timezone="Asia/Shanghai"
    )
    home = await manager.create(owner.actor_user_id, "小家")
    assert home.ledger.kind == LedgerKind.HOUSEHOLD_SHARED.value
    assert home.ledger.owner_user_id is None
    assert home.ledger.household_id == home.household.id
    assert home.membership.role == "owner"

    invitation = await manager.invite(
        owner.actor_user_id, home.household.id, "ou_house_member"
    )
    assert invitation.status == HouseholdInvitationStatus.PENDING.value
    with pytest.raises(HouseholdConflictError):
        await manager.invite(owner.actor_user_id, home.household.id, member.actor_user_id)
    with pytest.raises(HouseholdPermissionError):
        await manager.accept(outsider.actor_user_id, invitation.public_id)

    accepted = await manager.accept(member.actor_user_id, invitation.public_id)
    assert accepted.status == HouseholdInvitationStatus.ACCEPTED.value
    assert await manager.accept(member.actor_user_id, invitation.public_id) is accepted
    membership = await session.scalar(
        select(HouseholdMember).where(
            HouseholdMember.household_id == home.household.id,
            HouseholdMember.user_id == member.actor_user_id,
        )
    )
    assert membership is not None
    assert membership.status == HouseholdMemberStatus.ACTIVE.value

    shared = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_house_member",
    )
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="家庭晚餐",
        occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
    )
    await LedgerService(session, short_id_factory=lambda: "H0ME1").execute(shared, command)
    owner_shared = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="web",
        external_subject_id="ou_house_owner",
    )
    result = await LedgerService(session).execute(
        owner_shared, ParsedCommand(action=Action.LIST_ENTRIES)
    )
    assert "#H0ME1" in result.message

    with pytest.raises(LedgerAccessDeniedError):
        await LedgerService(session).execute(
            RequestContext(
                actor_user_id=member.actor_user_id,
                ledger_id=owner.ledger_id,
                source_channel="web",
            ),
            ParsedCommand(action=Action.LIST_ENTRIES),
        )
    with pytest.raises(LedgerAccessDeniedError):
        await LedgerService(session).execute(
            RequestContext(
                actor_user_id=outsider.actor_user_id,
                ledger_id=home.ledger.id,
                source_channel="web",
            ),
            ParsedCommand(action=Action.LIST_ENTRIES),
        )

    assert member.channel_identity_id is not None
    await LedgerManagementService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).select_for_channel(member.actor_user_id, member.channel_identity_id, home.ledger.id)
    await manager.leave(member.actor_user_id, home.household.id)
    refreshed = await _identity(session, "ou_house_member", "成员")
    assert refreshed.ledger_id == member.ledger_id
    with pytest.raises(LedgerAccessDeniedError):
        await LedgerService(session).execute(shared, ParsedCommand(action=Action.LIST_ENTRIES))
    entry = await session.scalar(select(LedgerEntry).where(LedgerEntry.short_id == "H0ME1"))
    assert entry is not None and entry.deleted_at is None and entry.ledger_id == home.ledger.id


@pytest.mark.asyncio
async def test_household_roles_rejection_cancellation_expiry_and_removal(
    session: AsyncSession,
) -> None:
    owner = await _identity(session, "ou_roles_owner", "所有者")
    member = await _identity(session, "ou_roles_member", "成员")
    target = await _identity(session, "ou_roles_target", "目标")
    manager = HouseholdManagementService(
        session,
        currency="CNY",
        timezone="Asia/Shanghai",
        invitation_ttl=timedelta(hours=1),
    )
    home = await manager.create(owner.actor_user_id, "家")
    first = await manager.invite(owner.actor_user_id, home.household.id, target.actor_user_id)
    rejected = await manager.reject(target.actor_user_id, first.id)
    assert rejected.status == HouseholdInvitationStatus.REJECTED.value

    second = await manager.invite(owner.actor_user_id, home.household.id, target.actor_user_id)
    cancelled = await manager.cancel_invitation(owner.actor_user_id, second.id)
    assert cancelled.status == HouseholdInvitationStatus.CANCELLED.value

    old = datetime(2026, 8, 8, tzinfo=UTC)
    third = await manager.invite(
        owner.actor_user_id, home.household.id, target.actor_user_id, now=old
    )
    with pytest.raises(HouseholdConflictError):
        await manager.accept(target.actor_user_id, third.id, now=old + timedelta(hours=2))
    assert third.status == HouseholdInvitationStatus.EXPIRED.value

    member_invite = await manager.invite(
        owner.actor_user_id, home.household.id, member.actor_user_id
    )
    await manager.accept(member.actor_user_id, member_invite.id)
    with pytest.raises(HouseholdPermissionError):
        await manager.invite(member.actor_user_id, home.household.id, target.actor_user_id)
    with pytest.raises(HouseholdPermissionError):
        await manager.remove_member(member.actor_user_id, home.household.id, owner.actor_user_id)
    with pytest.raises(HouseholdPermissionError):
        await manager.leave(owner.actor_user_id, home.household.id)
    await manager.remove_member(owner.actor_user_id, home.household.id, member.actor_user_id)
    with pytest.raises(LedgerNotFoundError):
        await LedgerManagementService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).get_accessible(member.actor_user_id, home.ledger.id)


@pytest.mark.asyncio
async def test_invalid_persisted_household_selection_falls_back(
    session: AsyncSession,
) -> None:
    owner = await _identity(session, "ou_fallback_owner", "所有者")
    member = await _identity(session, "ou_fallback_member", "成员")
    manager = HouseholdManagementService(
        session, currency="CNY", timezone="Asia/Shanghai"
    )
    home = await manager.create(owner.actor_user_id, "回退家庭")
    invitation = await manager.invite(
        owner.actor_user_id, home.household.id, member.actor_user_id
    )
    await manager.accept(member.actor_user_id, invitation.id)
    identity = await session.scalar(
        select(ChannelIdentity).where(ChannelIdentity.user_id == member.actor_user_id)
    )
    assert identity is not None
    identity.current_ledger_id = home.ledger.id
    membership = await session.scalar(
        select(HouseholdMember).where(
            HouseholdMember.household_id == home.household.id,
            HouseholdMember.user_id == member.actor_user_id,
        )
    )
    assert membership is not None
    membership.status = HouseholdMemberStatus.LEFT.value
    await session.flush()

    resolved = await _identity(session, "ou_fallback_member", "成员")
    assert resolved.ledger_id == member.ledger_id
    assert identity.current_ledger_id == member.ledger_id
