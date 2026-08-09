from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import (
    ChannelIdentity,
    DashboardSession,
    Household,
    HouseholdInvitation,
    HouseholdInvitationStatus,
    HouseholdMember,
    HouseholdMemberStatus,
    HouseholdRole,
    HouseholdStatus,
    Ledger,
    LedgerKind,
    User,
    UserStatus,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.ledger_management import normalize_ledger_name


class HouseholdManagementError(ValueError):
    pass


class HouseholdNotFoundError(HouseholdManagementError):
    pass


class HouseholdPermissionError(HouseholdManagementError):
    pass


class HouseholdConflictError(HouseholdManagementError):
    pass


@dataclass(frozen=True)
class HouseholdMemberView:
    membership: HouseholdMember
    user: User


@dataclass(frozen=True)
class HouseholdView:
    household: Household
    ledger: Ledger
    membership: HouseholdMember


class HouseholdManagementService:
    """Transactional lifecycle for households, members, invitations and their ledger."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        currency: str,
        timezone: str,
        invitation_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self._session = session
        self._currency = currency
        self._timezone = timezone
        self._invitation_ttl = invitation_ttl

    async def create(self, owner_user_id: uuid.UUID, name: str) -> HouseholdView:
        display, normalized = normalize_ledger_name(name)
        if await self._session.scalar(
            select(Household.id).where(
                Household.owner_user_id == owner_user_id,
                Household.normalized_name == normalized,
                Household.status == HouseholdStatus.ACTIVE.value,
            )
        ):
            raise HouseholdConflictError("已有同名或容易混淆的家庭空间")
        now = datetime.now(UTC)
        household = Household(
            owner_user_id=owner_user_id,
            name=display,
            normalized_name=normalized,
            status=HouseholdStatus.ACTIVE.value,
        )
        self._session.add(household)
        await self._session.flush()
        membership = HouseholdMember(
            household_id=household.id,
            user_id=owner_user_id,
            role=HouseholdRole.OWNER.value,
            status=HouseholdMemberStatus.ACTIVE.value,
            joined_at=now,
        )
        ledger = Ledger(
            owner_user_id=None,
            household_id=household.id,
            name=f"{display}公共账本",
            normalized_name=f"{normalized}公共账本",
            kind=LedgerKind.HOUSEHOLD_SHARED.value,
            currency=self._currency,
            timezone=self._timezone,
            is_default=False,
        )
        self._session.add_all([membership, ledger])
        await self._session.flush()
        await AccountService.create_default_for_ledger(self._session, ledger)
        return HouseholdView(household, ledger, membership)

    async def list_for_user(self, user_id: uuid.UUID) -> list[HouseholdView]:
        rows = (
            await self._session.execute(
                select(Household, Ledger, HouseholdMember)
                .join(HouseholdMember, HouseholdMember.household_id == Household.id)
                .join(
                    Ledger,
                    (Ledger.household_id == Household.id)
                    & (Ledger.kind == LedgerKind.HOUSEHOLD_SHARED.value),
                )
                .where(
                    HouseholdMember.user_id == user_id,
                    HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                    Household.status == HouseholdStatus.ACTIVE.value,
                )
                .order_by(Household.created_at, Household.id)
            )
        ).all()
        return [HouseholdView(*row) for row in rows]

    async def get(self, actor_user_id: uuid.UUID, household_id: uuid.UUID) -> HouseholdView:
        row = (
            await self._session.execute(
                select(Household, Ledger, HouseholdMember)
                .join(HouseholdMember, HouseholdMember.household_id == Household.id)
                .join(
                    Ledger,
                    (Ledger.household_id == Household.id)
                    & (Ledger.kind == LedgerKind.HOUSEHOLD_SHARED.value),
                )
                .where(
                    Household.id == household_id,
                    Household.status == HouseholdStatus.ACTIVE.value,
                    HouseholdMember.user_id == actor_user_id,
                    HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                )
            )
        ).first()
        if row is None:
            raise HouseholdNotFoundError("家庭空间不存在或当前用户不是有效成员")
        return HouseholdView(*row)

    async def find_by_name(self, actor_user_id: uuid.UUID, name: str) -> HouseholdView:
        _, normalized = normalize_ledger_name(name)
        matches = [
            item
            for item in await self.list_for_user(actor_user_id)
            if item.household.normalized_name == normalized
        ]
        if len(matches) != 1:
            raise HouseholdNotFoundError(f"未找到家庭空间“{name.strip()}”")
        return matches[0]

    async def list_members(
        self, actor_user_id: uuid.UUID, household_id: uuid.UUID
    ) -> list[HouseholdMemberView]:
        await self.get(actor_user_id, household_id)
        rows = (
            await self._session.execute(
                select(HouseholdMember, User)
                .join(User, User.id == HouseholdMember.user_id)
                .where(
                    HouseholdMember.household_id == household_id,
                    HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                )
                .order_by(HouseholdMember.role.desc(), HouseholdMember.joined_at, User.id)
            )
        ).all()
        return [HouseholdMemberView(*row) for row in rows]

    async def rename(
        self, actor_user_id: uuid.UUID, household_id: uuid.UUID, name: str
    ) -> HouseholdView:
        view = await self._require_owner(actor_user_id, household_id)
        display, normalized = normalize_ledger_name(name)
        conflict = await self._session.scalar(
            select(Household.id).where(
                Household.owner_user_id == actor_user_id,
                Household.normalized_name == normalized,
                Household.id != household_id,
                Household.status == HouseholdStatus.ACTIVE.value,
            )
        )
        if conflict is not None:
            raise HouseholdConflictError("已有同名或容易混淆的家庭空间")
        view.household.name = display
        view.household.normalized_name = normalized
        view.ledger.name = f"{display}公共账本"
        view.ledger.normalized_name = f"{normalized}公共账本"
        await self._session.flush()
        await self._session.refresh(view.household)
        await self._session.refresh(view.ledger)
        return view

    async def resolve_target(self, target: str) -> tuple[User, ChannelIdentity | None]:
        value = target.strip()
        identity = await self._session.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.channel == "feishu",
                ChannelIdentity.external_subject_id == value,
            )
        )
        user: User | None = None
        if identity is not None:
            user = await self._session.get(User, identity.user_id)
        else:
            try:
                target_id = uuid.UUID(value)
            except ValueError:
                target_id = None
            if target_id is not None:
                user = await self._session.get(User, target_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise HouseholdNotFoundError("邀请目标无法安全、唯一地解析为已有内部用户")
        return user, identity

    async def invite(
        self,
        actor_user_id: uuid.UUID,
        household_id: uuid.UUID,
        target: str | uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> HouseholdInvitation:
        await self._require_owner(actor_user_id, household_id)
        if isinstance(target, uuid.UUID):
            user = await self._session.get(User, target)
            identity = None
            if user is None or user.status != UserStatus.ACTIVE.value:
                raise HouseholdNotFoundError("邀请目标不是有效的内部用户")
        else:
            user, identity = await self.resolve_target(target)
        if user.id == actor_user_id:
            raise HouseholdConflictError("不能邀请自己加入家庭")
        member = await self._session.scalar(
            select(HouseholdMember).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.user_id == user.id,
                HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
            )
        )
        if member is not None:
            raise HouseholdConflictError("该用户已经是家庭成员")
        current = now or datetime.now(UTC)
        await self._expire_pending(current)
        active = await self._session.scalar(
            select(HouseholdInvitation).where(
                HouseholdInvitation.household_id == household_id,
                HouseholdInvitation.target_user_id == user.id,
                HouseholdInvitation.status == HouseholdInvitationStatus.PENDING.value,
            )
        )
        if active is not None:
            raise HouseholdConflictError("该用户已有待处理的家庭邀请")
        invitation = HouseholdInvitation(
            public_id=secrets.token_urlsafe(18),
            household_id=household_id,
            inviter_user_id=actor_user_id,
            target_user_id=user.id,
            target_channel_identity_id=identity.id if identity else None,
            status=HouseholdInvitationStatus.PENDING.value,
            expires_at=current + self._invitation_ttl,
        )
        self._session.add(invitation)
        await self._session.flush()
        return invitation

    async def list_invitations(
        self, target_user_id: uuid.UUID, *, now: datetime | None = None
    ) -> list[HouseholdInvitation]:
        await self._expire_pending(now or datetime.now(UTC))
        return list(
            (
                await self._session.scalars(
                    select(HouseholdInvitation)
                    .where(HouseholdInvitation.target_user_id == target_user_id)
                    .order_by(HouseholdInvitation.created_at.desc())
                )
            ).all()
        )

    async def accept(
        self,
        actor_user_id: uuid.UUID,
        invitation_ref: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> HouseholdInvitation:
        invitation = await self._get_invitation(invitation_ref, lock=True)
        if invitation.target_user_id != actor_user_id:
            raise HouseholdPermissionError("只有受邀用户可以接受该邀请")
        if invitation.status == HouseholdInvitationStatus.ACCEPTED.value:
            return invitation
        current = now or datetime.now(UTC)
        expires_at = invitation.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if invitation.status == HouseholdInvitationStatus.PENDING.value and expires_at <= current:
            invitation.status = HouseholdInvitationStatus.EXPIRED.value
            invitation.responded_at = current
        if invitation.status != HouseholdInvitationStatus.PENDING.value:
            raise HouseholdConflictError(f"该邀请当前状态为 {invitation.status}，不能接受")
        member = await self._session.scalar(
            select(HouseholdMember).where(
                HouseholdMember.household_id == invitation.household_id,
                HouseholdMember.user_id == actor_user_id,
            )
        )
        if member is None:
            member = HouseholdMember(
                household_id=invitation.household_id,
                user_id=actor_user_id,
                role=HouseholdRole.MEMBER.value,
                status=HouseholdMemberStatus.ACTIVE.value,
                joined_at=current,
            )
            self._session.add(member)
        else:
            member.role = HouseholdRole.MEMBER.value
            member.status = HouseholdMemberStatus.ACTIVE.value
            member.joined_at = current
        invitation.status = HouseholdInvitationStatus.ACCEPTED.value
        invitation.responded_at = current
        await self._session.flush()
        return invitation

    async def reject(
        self, actor_user_id: uuid.UUID, invitation_ref: uuid.UUID | str
    ) -> HouseholdInvitation:
        invitation = await self._get_invitation(invitation_ref, lock=True)
        if invitation.target_user_id != actor_user_id:
            raise HouseholdPermissionError("只有受邀用户可以拒绝该邀请")
        if invitation.status == HouseholdInvitationStatus.REJECTED.value:
            return invitation
        if invitation.status != HouseholdInvitationStatus.PENDING.value:
            raise HouseholdConflictError(f"该邀请当前状态为 {invitation.status}，不能拒绝")
        invitation.status = HouseholdInvitationStatus.REJECTED.value
        invitation.responded_at = datetime.now(UTC)
        await self._session.flush()
        return invitation

    async def cancel_invitation(
        self, actor_user_id: uuid.UUID, invitation_ref: uuid.UUID | str
    ) -> HouseholdInvitation:
        invitation = await self._get_invitation(invitation_ref, lock=True)
        await self._require_owner(actor_user_id, invitation.household_id)
        if invitation.status == HouseholdInvitationStatus.CANCELLED.value:
            return invitation
        if invitation.status != HouseholdInvitationStatus.PENDING.value:
            raise HouseholdConflictError(f"该邀请当前状态为 {invitation.status}，不能取消")
        invitation.status = HouseholdInvitationStatus.CANCELLED.value
        invitation.responded_at = datetime.now(UTC)
        await self._session.flush()
        return invitation

    async def leave(self, actor_user_id: uuid.UUID, household_id: uuid.UUID) -> None:
        view = await self.get(actor_user_id, household_id)
        if view.membership.role == HouseholdRole.OWNER.value:
            raise HouseholdPermissionError("家庭所有者不能直接退出；本阶段暂不开放所有权转移")
        view.membership.status = HouseholdMemberStatus.LEFT.value
        await self._reset_selections(actor_user_id, view.ledger.id)
        await self._session.flush()

    async def remove_member(
        self, actor_user_id: uuid.UUID, household_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        view = await self._require_owner(actor_user_id, household_id)
        member = await self._session.scalar(
            select(HouseholdMember).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.user_id == user_id,
                HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
            )
        )
        if member is None:
            raise HouseholdNotFoundError("家庭成员不存在")
        if member.role == HouseholdRole.OWNER.value:
            raise HouseholdPermissionError("不能移除家庭所有者")
        member.status = HouseholdMemberStatus.REMOVED.value
        await self._reset_selections(user_id, view.ledger.id)
        await self._session.flush()

    async def _require_owner(
        self, actor_user_id: uuid.UUID, household_id: uuid.UUID
    ) -> HouseholdView:
        view = await self.get(actor_user_id, household_id)
        if (
            view.household.owner_user_id != actor_user_id
            or view.membership.role != HouseholdRole.OWNER.value
        ):
            raise HouseholdPermissionError("只有家庭所有者可以执行此操作")
        return view

    async def _get_invitation(
        self, invitation_ref: uuid.UUID | str, *, lock: bool
    ) -> HouseholdInvitation:
        if isinstance(invitation_ref, uuid.UUID):
            condition = HouseholdInvitation.id == invitation_ref
        else:
            value = invitation_ref.strip()
            try:
                parsed = uuid.UUID(value)
            except ValueError:
                condition = HouseholdInvitation.public_id == value
            else:
                condition = or_(
                    HouseholdInvitation.id == parsed,
                    HouseholdInvitation.public_id == value,
                )
        stmt = select(HouseholdInvitation).where(condition)
        if lock:
            stmt = stmt.with_for_update()
        invitation = await self._session.scalar(stmt)
        if invitation is None:
            raise HouseholdNotFoundError("家庭邀请不存在")
        return invitation

    async def _expire_pending(self, now: datetime) -> None:
        await self._session.execute(
            update(HouseholdInvitation)
            .where(
                HouseholdInvitation.status == HouseholdInvitationStatus.PENDING.value,
                HouseholdInvitation.expires_at <= now,
            )
            .values(
                status=HouseholdInvitationStatus.EXPIRED.value,
                responded_at=now,
                updated_at=now,
            )
        )

    async def _reset_selections(
        self, user_id: uuid.UUID, inaccessible_ledger_id: uuid.UUID
    ) -> None:
        default_id = await self._session.scalar(
            select(Ledger.id).where(
                Ledger.owner_user_id == user_id,
                Ledger.kind == LedgerKind.PERSONAL.value,
                Ledger.is_default.is_(True),
            )
        )
        if default_id is None:
            raise HouseholdManagementError("成员没有可回退的默认个人账本")
        await self._session.execute(
            update(ChannelIdentity)
            .where(
                ChannelIdentity.user_id == user_id,
                ChannelIdentity.current_ledger_id == inaccessible_ledger_id,
            )
            .values(current_ledger_id=default_id)
        )
        await self._session.execute(
            update(DashboardSession)
            .where(
                DashboardSession.user_id == user_id,
                DashboardSession.ledger_id == inaccessible_ledger_id,
            )
            .values(ledger_id=default_id)
        )
