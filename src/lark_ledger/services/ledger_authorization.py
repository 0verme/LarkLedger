from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import (
    HouseholdMember,
    HouseholdMemberStatus,
    HouseholdStatus,
    Ledger,
    LedgerKind,
)


class LedgerAuthorizationError(PermissionError):
    """The actor is not an effective owner or household member for a ledger."""


class LedgerAuthorizationService:
    """Single authorization boundary for personal and household ledgers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_accessible(self, user_id: uuid.UUID, ledger_id: uuid.UUID) -> Ledger:
        # Keep the household status check explicit; this avoids callers having
        # to know how membership, ownership and ledger kind combine.
        ledger = await self._session.scalar(select(Ledger).where(Ledger.id == ledger_id))
        if ledger is None:
            raise LedgerAuthorizationError("账本不存在或当前用户无权访问")
        if ledger.kind == LedgerKind.PERSONAL.value:
            if ledger.owner_user_id == user_id and ledger.household_id is None:
                return ledger
        elif ledger.kind == LedgerKind.HOUSEHOLD_SHARED.value and ledger.household_id is not None:
            from lark_ledger.models import Household

            allowed = await self._session.scalar(
                select(HouseholdMember.id)
                .join(Household, Household.id == HouseholdMember.household_id)
                .where(
                    HouseholdMember.household_id == ledger.household_id,
                    HouseholdMember.user_id == user_id,
                    HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                    Household.status == HouseholdStatus.ACTIVE.value,
                )
            )
            if allowed is not None:
                return ledger
        raise LedgerAuthorizationError("账本不存在或当前用户无权访问")

    async def can_access(self, user_id: uuid.UUID, ledger_id: uuid.UUID) -> bool:
        try:
            await self.get_accessible(user_id, ledger_id)
        except LedgerAuthorizationError:
            return False
        return True

    async def list_accessible(self, user_id: uuid.UUID) -> list[Ledger]:
        from lark_ledger.models import Household

        member_households = (
            select(HouseholdMember.household_id)
            .join(Household, Household.id == HouseholdMember.household_id)
            .where(
                HouseholdMember.user_id == user_id,
                HouseholdMember.status == HouseholdMemberStatus.ACTIVE.value,
                Household.status == HouseholdStatus.ACTIVE.value,
            )
        )
        return list(
            (
                await self._session.scalars(
                    select(Ledger)
                    .where(
                        or_(
                            and_(
                                Ledger.kind == LedgerKind.PERSONAL.value,
                                Ledger.owner_user_id == user_id,
                            ),
                            and_(
                                Ledger.kind == LedgerKind.HOUSEHOLD_SHARED.value,
                                Ledger.household_id.in_(member_households),
                            ),
                        )
                    )
                    .order_by(Ledger.kind, Ledger.is_default.desc(), Ledger.created_at, Ledger.id)
                )
            ).all()
        )
