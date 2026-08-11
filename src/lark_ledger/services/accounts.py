from __future__ import annotations

import re
import unicodedata
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountStatus,
    AccountType,
    AccountVisibility,
    Ledger,
)
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.privacy import PrivacyService

DEFAULT_ACCOUNT_NAME = "默认账户"
MAX_ACCOUNT_NAME_LENGTH = 64


class AccountError(ValueError):
    pass


class AccountNotFoundError(AccountError):
    pass


class AccountConflictError(AccountError):
    pass


def normalize_account_name(value: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not display:
        raise AccountError("账户名称不能为空")
    if len(display) > MAX_ACCOUNT_NAME_LENGTH:
        raise AccountError(f"账户名称不能超过 {MAX_ACCOUNT_NAME_LENGTH} 个字符")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise AccountError("账户名称不能包含控制字符")
    normalized = re.sub(r"[\s\-_·•・.。]+", "", display).casefold()
    if not normalized:
        raise AccountError("账户名称无效")
    return display, normalized


class AccountService:
    """Deterministic account lifecycle constrained by ``RequestContext.ledger_id``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._authorization = LedgerAuthorizationService(session)
        self._privacy = PrivacyService(session)

    async def _visible_scope(self, context: RequestContext) -> Any | None:
        """Visibility filter, or ``None`` for personal ledgers (exact legacy behavior)."""
        if not await self._privacy.privacy_enabled(context):
            return None
        return self._privacy.account_visibility_scope(context)

    @staticmethod
    async def create_default_for_ledger(session: AsyncSession, ledger: Ledger) -> Account:
        existing = await session.scalar(
            select(Account).where(Account.ledger_id == ledger.id, Account.is_default.is_(True))
        )
        if existing is not None:
            return existing
        display, normalized = normalize_account_name(DEFAULT_ACCOUNT_NAME)
        account = Account(
            ledger_id=ledger.id,
            name=display,
            normalized_name=normalized,
            type=AccountType.CASH.value,
            currency=ledger.currency,
            opening_balance=Decimal("0"),
            status=AccountStatus.ACTIVE.value,
            is_default=True,
        )
        session.add(account)
        await session.flush()
        return account

    async def _authorize(self, context: RequestContext) -> Ledger:
        return await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)

    async def create(
        self,
        context: RequestContext,
        *,
        name: str,
        account_type: AccountType,
        subtype: str | None = None,
        provider: str | None = None,
        currency: str | None = None,
        opening_balance: Decimal = Decimal("0"),
        make_default: bool = False,
        visibility: AccountVisibility = AccountVisibility.SHARED,
    ) -> Account:
        ledger = await self._authorize(context)
        display, normalized = normalize_account_name(name)
        if await self._session.scalar(
            select(Account.id).where(
                Account.ledger_id == context.ledger_id,
                Account.normalized_name == normalized,
            )
        ):
            raise AccountConflictError("当前账本已有同名或容易混淆的账户")
        normalized_currency = (currency or ledger.currency).strip().upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise AccountError("币种必须是三位字母代码")
        if make_default:
            await self._clear_default(context.ledger_id)
        account = Account(
            ledger_id=context.ledger_id,
            name=display,
            normalized_name=normalized,
            type=account_type.value,
            subtype=(subtype or "").strip() or None,
            provider=(provider or "").strip() or None,
            currency=normalized_currency,
            opening_balance=opening_balance,
            status=AccountStatus.ACTIVE.value,
            is_default=make_default,
            visibility=visibility.value,
            owner_user_id=(
                context.actor_user_id if visibility == AccountVisibility.PRIVATE else None
            ),
        )
        self._session.add(account)
        await self._session.flush()
        return account

    async def list(
        self, context: RequestContext, *, include_archived: bool = False
    ) -> list[Account]:
        await self._authorize(context)
        query = select(Account).where(Account.ledger_id == context.ledger_id)
        visible = await self._visible_scope(context)
        if visible is not None:
            query = query.where(visible)
        if not include_archived:
            query = query.where(Account.status == AccountStatus.ACTIVE.value)
        return list(
            (
                await self._session.scalars(
                    query.order_by(Account.is_default.desc(), Account.created_at, Account.id)
                )
            ).all()
        )

    async def get(
        self, context: RequestContext, account_id: uuid.UUID, *, require_active: bool = False
    ) -> Account:
        await self._authorize(context)
        query = select(Account).where(
            Account.id == account_id,
            Account.ledger_id == context.ledger_id,
        )
        visible = await self._visible_scope(context)
        if visible is not None:
            query = query.where(visible)
        account = await self._session.scalar(query)
        if account is None or (require_active and account.status != AccountStatus.ACTIVE.value):
            raise AccountNotFoundError("账户不存在或不属于当前账本")
        return account

    async def get_default(self, context: RequestContext) -> Account:
        ledger = await self._authorize(context)
        query = select(Account).where(
            Account.ledger_id == context.ledger_id,
            Account.is_default.is_(True),
            Account.status == AccountStatus.ACTIVE.value,
        )
        visible = await self._visible_scope(context)
        if visible is not None:
            query = query.where(visible)
        account = await self._session.scalar(query)
        if account is None:
            account = await self.create_default_for_ledger(self._session, ledger)
        return account

    async def rename(self, context: RequestContext, account_id: uuid.UUID, name: str) -> Account:
        account = await self.get(context, account_id)
        display, normalized = normalize_account_name(name)
        conflict = await self._session.scalar(
            select(Account.id).where(
                Account.ledger_id == context.ledger_id,
                Account.normalized_name == normalized,
                Account.id != account.id,
            )
        )
        if conflict is not None:
            raise AccountConflictError("当前账本已有同名或容易混淆的账户")
        account.name = display
        account.normalized_name = normalized
        await self._session.flush()
        await self._session.refresh(account)
        return account

    async def archive(self, context: RequestContext, account_id: uuid.UUID) -> Account:
        account = await self.get(context, account_id)
        if account.is_default:
            raise AccountConflictError("默认账户不能归档，请先设置其他默认账户")
        account.status = AccountStatus.ARCHIVED.value
        await self._session.flush()
        await self._session.refresh(account)
        return account

    async def set_default(self, context: RequestContext, account_id: uuid.UUID) -> Account:
        account = await self.get(context, account_id, require_active=True)
        await self._clear_default(context.ledger_id)
        account.is_default = True
        await self._session.flush()
        await self._session.refresh(account)
        return account

    async def set_visibility(
        self,
        context: RequestContext,
        account_id: uuid.UUID,
        visibility: AccountVisibility,
    ) -> Account:
        """Toggle an account's visibility (P32), owner-only.

        Governance: the ledger owner (household owner / personal sole user) or
        the owner of a private account may change visibility. Marking an
        account private assigns ownership to the actor.
        """
        account = await self.get(context, account_id)
        from lark_ledger.services.member_resolution import MemberResolutionService

        roles = await MemberResolutionService(self._session).member_roles(context)
        ledger_owner = roles.get(context.actor_user_id) == "owner"
        owns_private = (
            account.owner_user_id is not None
            and account.owner_user_id == context.actor_user_id
        )
        if not (ledger_owner or owns_private):
            raise AccountNotFoundError("账户不存在或不属于当前账本")
        if visibility == AccountVisibility.PRIVATE:
            account.owner_user_id = context.actor_user_id
        else:
            account.owner_user_id = None
        account.visibility = visibility.value
        await self._session.flush()
        await self._session.refresh(account)
        return account

    async def _clear_default(self, ledger_id: uuid.UUID) -> None:
        await self._session.execute(
            update(Account)
            .where(Account.ledger_id == ledger_id, Account.is_default.is_(True))
            .values(is_default=False)
        )
        await self._session.flush()
