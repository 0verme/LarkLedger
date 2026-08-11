"""Account-level privacy for shared ledgers (P32).

``PrivacyService`` centralizes the visibility rules so every read and write
path enforces the same semantics:

* Privacy applies only to ``household_shared`` ledgers. Personal and legacy
  ledgers keep **exact** current behavior — every filter below is a no-op.
* ``shared`` accounts are visible to every active member of the household
  ledger; ``private`` accounts are visible only to their ``owner_user_id``.
* An entry is visible when it has no account (legacy rows) or its account is
  visible. This composes as an SQL ``EXISTS`` subquery so listing, analytics,
  budgets and exports never leak private rows even through side channels
  (category totals, budget spend, member stats).
* Transfers are visible iff the actor can see **both** accounts.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountVisibility,
    Ledger,
    LedgerEntry,
    PendingCommand,
)


class PrivacyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._kinds: dict[uuid.UUID, str | None] = {}

    async def ledger_kind(self, ledger_id: uuid.UUID) -> str | None:
        if ledger_id not in self._kinds:
            self._kinds[ledger_id] = await self._session.scalar(
                select(Ledger.kind).where(Ledger.id == ledger_id)
            )
        return self._kinds[ledger_id]

    async def privacy_enabled(self, context: RequestContext) -> bool:
        """Privacy is a household-ledger concern; personal ledgers are untouched."""
        return await self.ledger_kind(context.ledger_id) == "household_shared"

    def _account_visible_condition(self, context: RequestContext) -> Any:
        return or_(
            Account.visibility == AccountVisibility.SHARED.value,
            and_(
                Account.visibility == AccountVisibility.PRIVATE.value,
                Account.owner_user_id == context.actor_user_id,
            ),
        )

    def account_visibility_scope(self, context: RequestContext) -> Any:
        """SQL condition selecting ``Account`` rows the actor may see."""
        return self._account_visible_condition(context)

    def account_visible_exists(
        self, context: RequestContext, account_id_column: Any
    ) -> Any:
        """SQL ``EXISTS`` subquery: the account referenced by
        ``account_id_column`` is visible to the actor."""
        return exists(
            select(1).where(
                Account.id == account_id_column,
                self._account_visible_condition(context),
            )
        )

    async def entry_visibility_scope(self, context: RequestContext) -> Any | None:
        """Extra SQL condition for ``ledger_entries`` queries, or ``None`` when
        privacy does not apply (personal ledger). Entries without an account
        (legacy rows) are always visible."""
        if not await self.privacy_enabled(context):
            return None
        return or_(
            LedgerEntry.account_id.is_(None),
            self.account_visible_exists(context, LedgerEntry.account_id),
        )

    async def pending_visibility_scope(self, context: RequestContext) -> Any | None:
        """Extra SQL condition for ``pending_commands`` queries, or ``None`` when
        privacy does not apply. A pending is visible when every account it
        targets (``account_id``, or the ``from``/``to`` pair) is visible."""
        if not await self.privacy_enabled(context):
            return None
        return and_(
            or_(
                PendingCommand.account_id.is_(None),
                self.account_visible_exists(context, PendingCommand.account_id),
            ),
            or_(
                PendingCommand.from_account_id.is_(None),
                self.account_visible_exists(context, PendingCommand.from_account_id),
            ),
            or_(
                PendingCommand.to_account_id.is_(None),
                self.account_visible_exists(context, PendingCommand.to_account_id),
            ),
        )

    async def visible_account_ids(self, context: RequestContext) -> set[uuid.UUID]:
        """In-Python set of account ids visible to the actor (transfers/stats)."""
        if not await self.privacy_enabled(context):
            return set()
        rows = await self._session.scalars(
            select(Account.id).where(
                Account.ledger_id == context.ledger_id,
                self._account_visible_condition(context),
            )
        )
        return {row for row in rows}

    async def can_view_account(
        self, context: RequestContext, account_id: uuid.UUID
    ) -> bool:
        """True when the actor may see this specific account (404 semantics)."""
        if not await self.privacy_enabled(context):
            return True
        return await self._session.scalar(
            select(Account.id).where(
                Account.id == account_id,
                Account.ledger_id == context.ledger_id,
                self._account_visible_condition(context),
            )
        ) is not None
