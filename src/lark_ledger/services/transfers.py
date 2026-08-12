from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountStatus,
    AccountType,
    Direction,
    LedgerEntry,
    Transfer,
    TransferRevision,
)
from lark_ledger.services.accounts import AccountService, normalize_account_name
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.privacy import PrivacyService


class TransferError(ValueError):
    pass


class TransferNotFoundError(TransferError):
    pass


class TransferConflictError(TransferError):
    pass


class AccountHintAmbiguousError(TransferError):
    pass


@dataclass(frozen=True, slots=True)
class AccountBalance:
    account_id: uuid.UUID
    ledger_id: uuid.UUID
    account_name: str
    account_type: AccountType
    currency: str
    opening_balance: Decimal
    current_balance: Decimal
    archived: bool


@dataclass(frozen=True, slots=True)
class AssetSummary:
    ledger_id: uuid.UUID
    currency: str
    total_assets: Decimal
    total_liabilities: Decimal
    net_assets: Decimal
    accounts: list[AccountBalance]


def snapshot_transfer(row: Transfer) -> dict[str, Any]:
    return {
        "snapshot_version": 1,
        "transfer_id": str(row.id),
        "ledger_id": str(row.ledger_id),
        "from_account_id": str(row.from_account_id),
        "to_account_id": str(row.to_account_id),
        "amount": format(row.amount, "f"),
        "currency": row.currency,
        "note": row.note,
        "occurred_at": row.occurred_at.isoformat(),
        "reversed_at": row.reversed_at.isoformat() if row.reversed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class TransferService:
    """Deterministic transfer commands and derived account balance queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._authorization = LedgerAuthorizationService(session)

    async def resolve_account_hint(self, context: RequestContext, hint: str) -> Account:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        _, normalized = normalize_account_name(hint)
        query = select(Account).where(
            Account.ledger_id == context.ledger_id,
            Account.status == AccountStatus.ACTIVE.value,
            Account.normalized_name == normalized,
        )
        # P39 §48/§49: account resolution must stay inside the actor's visible
        # accounts. A private account owned by another household member must not
        # resolve, and the ambiguous message must not reveal its existence.
        privacy = PrivacyService(self._session)
        if await privacy.privacy_enabled(context):
            query = query.where(privacy.account_visibility_scope(context))
        rows = list((await self._session.scalars(query)).all())
        if len(rows) != 1:
            raise AccountHintAmbiguousError("账户提示无法唯一解析，请确认准确的账户名称")
        return rows[0]

    async def create(
        self,
        context: RequestContext,
        *,
        from_account_id: uuid.UUID,
        to_account_id: uuid.UUID,
        amount: Decimal,
        occurred_at: datetime,
        note: str = "",
        source_type: str = "client",
        source_message_id: str | None = None,
        transfer_id: uuid.UUID | None = None,
    ) -> Transfer:
        ledger = await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        if amount <= 0:
            raise TransferError("转账金额必须大于 0")
        accounts = AccountService(self._session)
        source = await accounts.get(context, from_account_id, require_active=True)
        target = await accounts.get(context, to_account_id, require_active=True)
        if source.id == target.id:
            raise TransferConflictError("转出和转入账户不能相同")
        if source.currency != target.currency or source.currency != ledger.currency:
            raise TransferConflictError("P27 仅支持账本本位币账户之间的转账")
        row = Transfer(
            id=transfer_id or uuid.uuid4(),
            ledger_id=context.ledger_id,
            from_account_id=source.id,
            to_account_id=target.id,
            actor_user_id=context.actor_user_id,
            amount=amount,
            currency=ledger.currency,
            note=note.strip(),
            occurred_at=occurred_at,
            source_type=source_type,
            source_message_id=source_message_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, context: RequestContext, transfer_id: uuid.UUID) -> Transfer:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        filters = [
            Transfer.id == transfer_id,
            Transfer.ledger_id == context.ledger_id,
        ]
        visible = await self._both_accounts_visible(context)
        if visible is not None:
            filters.append(visible)
        row = await self._session.scalar(select(Transfer).where(*filters))
        if row is None:
            raise TransferNotFoundError("转账不存在或不属于当前账本")
        return row

    async def list_paginated(
        self, context: RequestContext, *, page: int, page_size: int
    ) -> tuple[list[Transfer], int]:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        filters = [Transfer.ledger_id == context.ledger_id]
        visible = await self._both_accounts_visible(context)
        if visible is not None:
            filters.append(visible)
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(Transfer).where(*filters)
            )
            or 0
        )
        rows = (
            (
                await self._session.scalars(
                    select(Transfer)
                    .where(*filters)
                    .order_by(
                        Transfer.occurred_at.desc(),
                        Transfer.created_at.desc(),
                        Transfer.id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .all()
        )
        return list(rows), total

    async def _both_accounts_visible(self, context: RequestContext) -> Any | None:
        """P32: a transfer is visible iff the actor can see both accounts."""
        from lark_ledger.services.privacy import PrivacyService

        privacy = PrivacyService(self._session)
        if not await privacy.privacy_enabled(context):
            return None
        return and_(
            privacy.account_visible_exists(context, Transfer.from_account_id),
            privacy.account_visible_exists(context, Transfer.to_account_id),
        )

    async def revisions(
        self, context: RequestContext, transfer_id: uuid.UUID
    ) -> list[TransferRevision]:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        rows = (
            (
                await self._session.scalars(
                    select(TransferRevision)
                    .where(
                        TransferRevision.transfer_id == transfer_id,
                        TransferRevision.ledger_id == context.ledger_id,
                    )
                    .order_by(TransferRevision.created_at.desc())
                    .limit(100)
                )
            )
            .all()
        )
        return list(rows)

    async def reverse(self, context: RequestContext, transfer_id: uuid.UUID) -> Transfer:
        row = await self._locked(context, transfer_id)
        if row.reversed_at is not None:
            raise TransferConflictError("该转账已经撤销")
        before = snapshot_transfer(row)
        row.reversed_at = datetime.now(UTC)
        row.updated_at = row.reversed_at
        await self._session.flush()
        self._add_revision(context, row, "reverse", before, snapshot_transfer(row))
        await self._session.flush()
        return row

    async def revise(
        self,
        context: RequestContext,
        transfer_id: uuid.UUID,
        *,
        amount: Decimal,
        occurred_at: datetime | None = None,
        note: str | None = None,
    ) -> Transfer:
        row = await self._locked(context, transfer_id)
        if row.reversed_at is not None:
            raise TransferConflictError("已撤销转账不能修改")
        if amount <= 0:
            raise TransferError("转账金额必须大于 0")
        before = snapshot_transfer(row)
        row.amount = amount
        if occurred_at is not None:
            row.occurred_at = occurred_at
        if note is not None:
            row.note = note.strip()
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        self._add_revision(context, row, "update", before, snapshot_transfer(row))
        await self._session.flush()
        return row

    async def account_balance(
        self, context: RequestContext, account_id: uuid.UUID
    ) -> AccountBalance:
        account = await AccountService(self._session).get(context, account_id)
        entry_total = Decimal(
            await self._session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.INCOME, LedgerEntry.amount),
                                else_=-LedgerEntry.amount,
                            )
                        ),
                        0,
                    )
                ).where(
                    LedgerEntry.ledger_id == context.ledger_id,
                    LedgerEntry.account_id == account.id,
                    LedgerEntry.deleted_at.is_(None),
                )
            )
            or 0
        )
        transfer_total = Decimal(
            await self._session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (Transfer.to_account_id == account.id, Transfer.amount),
                                else_=-Transfer.amount,
                            )
                        ),
                        0,
                    )
                ).where(
                    Transfer.ledger_id == context.ledger_id,
                    Transfer.reversed_at.is_(None),
                    or_(
                        Transfer.from_account_id == account.id,
                        Transfer.to_account_id == account.id,
                    ),
                )
            )
            or 0
        )
        # Liability balances use the inverse sign: positive means debt owed.
        movement = entry_total + transfer_total
        current = (
            account.opening_balance - movement
            if account.type == AccountType.LIABILITY.value
            else account.opening_balance + movement
        )
        return AccountBalance(
            account_id=account.id,
            ledger_id=account.ledger_id,
            account_name=account.name,
            account_type=AccountType(account.type),
            currency=account.currency,
            opening_balance=account.opening_balance,
            current_balance=current,
            archived=account.status == AccountStatus.ARCHIVED.value,
        )

    async def asset_summary(self, context: RequestContext) -> AssetSummary:
        ledger = await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        accounts = await AccountService(self._session).list(context, include_archived=True)
        balances = [await self.account_balance(context, account.id) for account in accounts]
        assets = sum(
            (row.current_balance for row in balances if row.account_type != AccountType.LIABILITY),
            Decimal("0"),
        )
        liabilities = sum(
            (row.current_balance for row in balances if row.account_type == AccountType.LIABILITY),
            Decimal("0"),
        )
        return AssetSummary(
            ledger_id=context.ledger_id,
            currency=ledger.currency,
            total_assets=assets,
            total_liabilities=liabilities,
            net_assets=assets - liabilities,
            accounts=balances,
        )

    async def _locked(self, context: RequestContext, transfer_id: uuid.UUID) -> Transfer:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        row = await self._session.scalar(
            select(Transfer)
            .where(Transfer.id == transfer_id, Transfer.ledger_id == context.ledger_id)
            .with_for_update()
        )
        if row is None:
            raise TransferNotFoundError("转账不存在或不属于当前账本")
        return row

    def _add_revision(
        self,
        context: RequestContext,
        row: Transfer,
        change_type: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self._session.add(
            TransferRevision(
                transfer_id=row.id,
                ledger_id=context.ledger_id,
                actor_user_id=context.actor_user_id,
                change_type=change_type,
                before_json=before,
                after_json=after,
            )
        )
