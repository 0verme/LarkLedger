"""Member-dimension contribution statistics (P30).

``MemberStatsService.stats`` aggregates confirmed ``ledger_entries`` grouped by
``paid_by_user_id`` — who actually paid — for the current ledger. Transfers
live in a separate table and are never counted as income, expense, or budget
usage. For household ledgers only active members appear (a payer who left the
household is omitted from the stats rather than shown with stale data); for
personal ledgers the single owner is returned.

Privacy (P32) filters the underlying entries to accounts visible to the actor,
so one member's private spending never leaks into another member's stats.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.services.member_resolution import MemberResolutionService
from lark_ledger.web_schemas import MemberStats


class MemberStatsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stats(
        self,
        context: RequestContext,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        privacy_filter: Any = None,
    ) -> list[MemberStats]:
        """Aggregate confirmed entries by payer for the current ledger.

        ``privacy_filter`` is an optional extra SQLAlchemy condition (added by
        P32) that restricts entries to accounts visible to ``context.actor``.
        """
        filters: list[Any] = [
            LedgerEntry.ledger_id == context.ledger_id,
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.paid_by_user_id.is_not(None),
        ]
        if start is not None:
            filters.append(LedgerEntry.occurred_at >= start)
        if end is not None:
            filters.append(LedgerEntry.occurred_at < end)
        if privacy_filter is not None:
            filters.append(privacy_filter)
        rows = (
            await self._session.execute(
                select(
                    LedgerEntry.paid_by_user_id,
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.EXPENSE, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.INCOME, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.count(LedgerEntry.id),
                )
                .where(*filters)
                .group_by(LedgerEntry.paid_by_user_id)
            )
        ).all()
        if not rows:
            return []

        resolver = MemberResolutionService(self._session)
        members = await resolver.ledger_members(context)
        by_id: dict[uuid.UUID, object] = {user.id: user for user in members}
        roles = await resolver.member_roles(context)
        stats: list[MemberStats] = []
        for payer_id, expense_total, income_total, tx_count in rows:
            user = by_id.get(payer_id)
            if user is None:
                # Payer is no longer an active member of this ledger; omit
                # rather than surface stale contribution data.
                continue
            alias = await resolver.member_alias(context, payer_id)
            stats.append(
                MemberStats(
                    user_id=str(payer_id),
                    display_name=getattr(user, "display_name", "") or "",
                    alias=alias,
                    role=roles.get(payer_id, "member"),
                    expense_total=Decimal(expense_total),
                    income_total=Decimal(income_total),
                    transaction_count=int(tx_count),
                )
            )
        stats.sort(key=lambda item: (-item.expense_total, item.user_id))
        return stats
