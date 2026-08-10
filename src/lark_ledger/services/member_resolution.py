"""Deterministic household-member payer resolution (P30).

A ``payer_reference`` is whatever string the AI echoed from the user ("老婆",
"爸爸", a display name, an open_id, or a UUID). The ledger resolves it to an
internal user through deterministic matching only — never through AI, never
through guesswork:

* ``None`` / empty → the acting user pays.
* a literal user UUID → must be a ledger member.
* a household member ``alias`` (exact, normalized) → the alias owner.
* a member ``display_name`` (exact, normalized) → that member.
* a Feishu ``open_id`` → the member owning that channel identity.

0 matches → ``PayerResolutionError`` listing the available payers; >1 matches →
``PayerResolutionError`` asking for a more specific reference. Personal ledgers
resolve against their single owner, so existing behavior is unchanged.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    ChannelIdentity,
    Household,
    HouseholdMember,
    HouseholdMemberStatus,
    HouseholdStatus,
    Ledger,
    LedgerKind,
    User,
    UserStatus,
)


class PayerResolutionError(ValueError):
    """The payer reference could not be resolved to exactly one ledger member."""


def _normalize(value: str) -> str:
    """Normalize a name/reference for exact comparison (NFKC, folded, compact)."""
    return re.sub(r"[\s　\-_·•・.。]+", "", unicodedata.normalize("NFKC", value)).casefold()


MAX_ALIAS_LENGTH = 32


def normalize_alias(value: str) -> str:
    """Validate and normalize a payer alias (``老婆`` / ``爸爸``).

    Returns the display form (single-spaced NFKC, stripped). Raises
    ``ValueError`` for empty, overlong, or control-character aliases.
    """
    display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not display:
        raise ValueError("付款人称呼不能为空")
    if len(display) > MAX_ALIAS_LENGTH:
        raise ValueError(f"付款人称呼不能超过 {MAX_ALIAS_LENGTH} 个字符")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise ValueError("付款人称呼不能包含控制字符")
    return display


class MemberResolutionService:
    """Resolve payer references against the ledger's active members."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ledger_members(self, context: RequestContext) -> list[User]:
        """Active members (owner first) for the ledger's household / personal owner."""
        ledger = await self._session.get(Ledger, context.ledger_id)
        if ledger is None:
            return []
        if ledger.kind == LedgerKind.PERSONAL.value and ledger.owner_user_id is not None:
            owner = await self._session.get(User, ledger.owner_user_id)
            if owner is not None and owner.status == UserStatus.ACTIVE.value:
                return [owner]
            return []
        if ledger.kind == LedgerKind.HOUSEHOLD_SHARED.value and ledger.household_id is not None:
            rows = (
                await self._session.execute(
                    select(User, HouseholdMember.alias)
                    .join(HouseholdMember, HouseholdMember.user_id == User.id)
                    .join(Household, Household.id == HouseholdMember.household_id)
                    .where(
                        HouseholdMember.household_id == ledger.household_id,
                        HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                        Household.status == HouseholdStatus.ACTIVE.value,
                        User.status == UserStatus.ACTIVE.value,
                    )
                    .order_by(
                        HouseholdMember.role.desc(),
                        User.display_name,
                        User.id,
                    )
                )
            ).all()
            return [user for user, _ in rows]
        return []

    async def _member_aliases(self, context: RequestContext) -> dict[uuid.UUID, str | None]:
        ledger = await self._session.get(Ledger, context.ledger_id)
        if ledger is None or ledger.household_id is None:
            return {}
        rows = (
            await self._session.execute(
                select(HouseholdMember.user_id, HouseholdMember.alias).where(
                    HouseholdMember.household_id == ledger.household_id,
                    HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                )
            )
        ).all()
        return {user_id: alias for user_id, alias in rows}

    async def member_alias(
        self, context: RequestContext, user_id: uuid.UUID
    ) -> str | None:
        """Return the member's payer alias in this ledger, or ``None``."""
        return (await self._member_aliases(context)).get(user_id)

    async def member_display_names(
        self, context: RequestContext, user_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, str | None]:
        """Best-effort display name (alias > display_name) per member, one query."""
        if not user_ids:
            return {}
        aliases = await self._member_aliases(context)
        members = await self.ledger_members(context)
        result: dict[uuid.UUID, str | None] = {}
        for user in members:
            if user.id in user_ids:
                alias = aliases.get(user.id)
                result[user.id] = alias or user.display_name or None
        return result

    async def member_roles(self, context: RequestContext) -> dict[uuid.UUID, str]:
        """Map ledger members to their role (``owner`` / ``member``)."""
        ledger = await self._session.get(Ledger, context.ledger_id)
        if ledger is None:
            return {}
        if ledger.kind == LedgerKind.PERSONAL.value and ledger.owner_user_id is not None:
            return {ledger.owner_user_id: "owner"}
        if ledger.household_id is None:
            return {}
        rows = (
            await self._session.execute(
                select(HouseholdMember.user_id, HouseholdMember.role).where(
                    HouseholdMember.household_id == ledger.household_id,
                    HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                )
            )
        ).all()
        return {user_id: role for user_id, role in rows}

    async def is_member(self, context: RequestContext, user_id: uuid.UUID) -> bool:
        return user_id in {user.id for user in await self.ledger_members(context)}

    async def resolve_payer(
        self, context: RequestContext, reference: str | None
    ) -> uuid.UUID:
        """Return the payer's user id for ``reference``; defaults to the actor."""
        value = (reference or "").strip()
        if not value:
            return context.actor_user_id
        members = await self.ledger_members(context)
        if not members:
            raise PayerResolutionError("当前账本没有可指定付款人的成员")
        aliases = await self._member_aliases(context)

        try:
            target = uuid.UUID(value)
        except ValueError:
            target = None
        if target is not None:
            matched = [user for user in members if user.id == target]
            if len(matched) == 1:
                return matched[0].id
            raise PayerResolutionError("该付款人不存在或不属于当前账本")

        normalized = _normalize(value)
        alias_matches = [
            user
            for user in members
            if (alias := aliases.get(user.id)) and _normalize(alias) == normalized
        ]
        if len(alias_matches) == 1:
            return alias_matches[0].id
        if len(alias_matches) > 1:
            raise PayerResolutionError("付款人称呼不唯一，请使用更明确的名称")

        name_matches = [
            user
            for user in members
            if user.display_name and _normalize(user.display_name) == normalized
        ]
        if len(name_matches) == 1:
            return name_matches[0].id
        if len(name_matches) > 1:
            raise PayerResolutionError("付款人称呼不唯一，请使用更明确的名称")

        identity_rows = (
            await self._session.execute(
                select(ChannelIdentity.user_id).where(
                    ChannelIdentity.channel == "feishu",
                    ChannelIdentity.external_subject_id == value,
                    ChannelIdentity.user_id.in_([user.id for user in members]),
                )
            )
        ).all()
        if len(identity_rows) == 1:
            return cast(uuid.UUID, identity_rows[0][0])

        available = [
            alias or user.display_name or f"用户 {str(user.id)[:8]}"
            for user in members
            for alias in [aliases.get(user.id)]
        ]
        raise PayerResolutionError(
            f"无法识别付款人“{value.strip()}”。"
            f"当前账本的成员：{'、'.join(available)}"
        )
