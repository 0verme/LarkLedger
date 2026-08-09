from __future__ import annotations

import re
import unicodedata
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import ChannelIdentity, DashboardSession, Ledger, LedgerKind
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.ledger_authorization import (
    LedgerAuthorizationError,
    LedgerAuthorizationService,
)

MAX_LEDGER_NAME_LENGTH = 64


class LedgerManagementError(ValueError):
    pass


class LedgerNotFoundError(LedgerManagementError):
    pass


class LedgerNameConflictError(LedgerManagementError):
    pass


def normalize_ledger_name(value: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not display:
        raise LedgerManagementError("账本名称不能为空")
    if len(display) > MAX_LEDGER_NAME_LENGTH:
        raise LedgerManagementError(f"账本名称不能超过 {MAX_LEDGER_NAME_LENGTH} 个字符")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise LedgerManagementError("账本名称不能包含控制字符")
    key = re.sub(r"[\s\-_·•・.。]+", "", display).casefold()
    if not key:
        raise LedgerManagementError("账本名称无效")
    return display, key


class LedgerManagementService:
    """Own personal ledgers and resolve deterministic per-entry selections."""

    def __init__(self, session: AsyncSession, *, currency: str, timezone: str) -> None:
        self._session = session
        self._currency = currency
        self._timezone = timezone

    async def list_owned(self, user_id: uuid.UUID) -> list[Ledger]:
        return list(
            (
                await self._session.scalars(
                    select(Ledger)
                    .where(
                        Ledger.owner_user_id == user_id,
                        Ledger.kind == LedgerKind.PERSONAL.value,
                    )
                    .order_by(Ledger.is_default.desc(), Ledger.created_at, Ledger.id)
                )
            ).all()
        )

    async def list_accessible(self, user_id: uuid.UUID) -> list[Ledger]:
        return await LedgerAuthorizationService(self._session).list_accessible(user_id)

    async def get_accessible(self, user_id: uuid.UUID, ledger_id: uuid.UUID) -> Ledger:
        try:
            return await LedgerAuthorizationService(self._session).get_accessible(
                user_id, ledger_id
            )
        except LedgerAuthorizationError as exc:
            raise LedgerNotFoundError("账本不存在或当前用户无权访问") from exc

    async def get_owned(self, user_id: uuid.UUID, ledger_id: uuid.UUID) -> Ledger:
        ledger = await self._session.scalar(
            select(Ledger).where(Ledger.id == ledger_id, Ledger.owner_user_id == user_id)
        )
        if ledger is None or ledger.kind != LedgerKind.PERSONAL.value:
            raise LedgerNotFoundError("账本不存在或不属于当前用户")
        return ledger

    async def get_default(self, user_id: uuid.UUID) -> Ledger:
        ledger = await self._session.scalar(
            select(Ledger).where(
                Ledger.owner_user_id == user_id,
                Ledger.kind == LedgerKind.PERSONAL.value,
                Ledger.is_default.is_(True),
            )
        )
        if ledger is None:
            raise LedgerNotFoundError("当前用户没有默认账本")
        return ledger

    async def find_owned_by_name(self, user_id: uuid.UUID, name: str) -> Ledger:
        _, normalized = normalize_ledger_name(name)
        ledger = await self._session.scalar(
            select(Ledger).where(
                Ledger.owner_user_id == user_id,
                Ledger.normalized_name == normalized,
            )
        )
        if ledger is None:
            raise LedgerNotFoundError(f"未找到账本“{name.strip()}”")
        return ledger

    async def create(self, user_id: uuid.UUID, name: str) -> Ledger:
        display, normalized = normalize_ledger_name(name)
        existing = await self._session.scalar(
            select(Ledger.id).where(
                Ledger.owner_user_id == user_id,
                Ledger.normalized_name == normalized,
            )
        )
        if existing is not None:
            raise LedgerNameConflictError("已有同名或容易混淆的账本")
        ledger = Ledger(
            owner_user_id=user_id,
            name=display,
            normalized_name=normalized,
            kind=LedgerKind.PERSONAL.value,
            currency=self._currency,
            timezone=self._timezone,
            is_default=False,
        )
        self._session.add(ledger)
        await self._session.flush()
        await AccountService.create_default_for_ledger(self._session, ledger)
        return ledger

    async def rename(self, user_id: uuid.UUID, ledger_id: uuid.UUID, name: str) -> Ledger:
        ledger = await self.get_owned(user_id, ledger_id)
        display, normalized = normalize_ledger_name(name)
        conflict = await self._session.scalar(
            select(Ledger.id).where(
                Ledger.owner_user_id == user_id,
                Ledger.normalized_name == normalized,
                Ledger.id != ledger.id,
            )
        )
        if conflict is not None:
            raise LedgerNameConflictError("已有同名或容易混淆的账本")
        ledger.name = display
        ledger.normalized_name = normalized
        await self._session.flush()
        return ledger

    async def set_default(self, user_id: uuid.UUID, ledger_id: uuid.UUID) -> Ledger:
        ledger = await self.get_owned(user_id, ledger_id)
        await self._session.execute(
            update(Ledger)
            .where(Ledger.owner_user_id == user_id, Ledger.is_default.is_(True))
            .values(is_default=False)
        )
        await self._session.flush()
        ledger.is_default = True
        await self._session.flush()
        return ledger

    async def select_for_channel(
        self, user_id: uuid.UUID, identity_id: uuid.UUID, ledger_id: uuid.UUID
    ) -> Ledger:
        ledger = await self.get_accessible(user_id, ledger_id)
        identity = await self._session.get(ChannelIdentity, identity_id)
        if identity is None or identity.user_id != user_id:
            raise LedgerNotFoundError("入口身份不存在或不属于当前用户")
        identity.current_ledger_id = ledger.id
        await self._session.flush()
        return ledger

    async def select_for_session(
        self, user_id: uuid.UUID, session_id: uuid.UUID, ledger_id: uuid.UUID
    ) -> Ledger:
        ledger = await self.get_accessible(user_id, ledger_id)
        dashboard_session = await self._session.get(DashboardSession, session_id)
        if dashboard_session is None or dashboard_session.user_id != user_id:
            raise LedgerNotFoundError("Dashboard 会话不存在或不属于当前用户")
        dashboard_session.ledger_id = ledger.id
        await self._session.flush()
        return ledger
