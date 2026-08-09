"""Safe, user-scoped Pending Console read models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.confirmation_id import format_confirmation_ref, normalize_confirmation_code
from lark_ledger.context import RequestContext
from lark_ledger.models import PendingCommand, PendingStatus
from lark_ledger.services.pending import PendingPreview
from lark_ledger.web_schemas import PendingDetail, PendingGroup, PendingPage, WebPending


class WebPendingQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_pending(
        self,
        user_open_id: RequestContext | str,
        *,
        group: PendingGroup,
        page: int,
        page_size: int,
        now: datetime | None = None,
    ) -> PendingPage:
        current = now or datetime.now(UTC)
        filters = [self._scope(user_open_id)]
        if group == "pending":
            filters.extend(
                [
                    PendingCommand.status.in_(["pending", "executing"]),
                    PendingCommand.expires_at > current,
                ]
            )
        elif group == "completed":
            filters.append(PendingCommand.status == PendingStatus.EXECUTED.value)
        else:
            filters.append(
                or_(
                    PendingCommand.status.in_(["cancelled", "expired", "failed"]),
                    and_(
                        PendingCommand.status == PendingStatus.PENDING.value,
                        PendingCommand.expires_at <= current,
                    ),
                )
            )
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(PendingCommand).where(*filters)
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(PendingCommand)
                .where(*filters)
                .order_by(PendingCommand.created_at.desc(), PendingCommand.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return PendingPage(
            items=[self._pending(row, now=current) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            pages=ceil(total / page_size) if total else 0,
        )

    async def detail(
        self,
        user_open_id: RequestContext | str,
        confirmation_id: str,
        *,
        now: datetime | None = None,
    ) -> PendingDetail | None:
        code = normalize_confirmation_code(confirmation_id)
        row = await self._session.scalar(
            select(PendingCommand).where(
                self._scope(user_open_id),
                PendingCommand.confirmation_code == code,
            )
        )
        if row is None:
            return None
        preview = PendingPreview.from_json(row.preview_json)
        return PendingDetail(
            pending=self._pending(row, now=now or datetime.now(UTC)),
            preview=preview.as_json(),
        )

    @staticmethod
    def _scope(scope: RequestContext | str) -> Any:
        if isinstance(scope, str):
            return PendingCommand.user_open_id == scope
        if scope.external_subject_id is None:
            return and_(
                PendingCommand.actor_user_id == scope.actor_user_id,
                PendingCommand.ledger_id == scope.ledger_id,
            )
        return or_(
            and_(
                PendingCommand.actor_user_id == scope.actor_user_id,
                PendingCommand.ledger_id == scope.ledger_id,
            ),
            and_(
                PendingCommand.actor_user_id.is_(None),
                PendingCommand.ledger_id.is_(None),
                PendingCommand.user_open_id == scope.external_subject_id,
            ),
        )

    @staticmethod
    def _pending(row: PendingCommand, *, now: datetime) -> WebPending:
        preview = PendingPreview.from_json(row.preview_json)
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        effective_status = row.status
        if effective_status == PendingStatus.PENDING.value and expires_at <= now:
            effective_status = PendingStatus.EXPIRED.value
        completed_at = row.executed_at or row.cancelled_at or row.confirmed_at
        if completed_at is not None and completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        return WebPending(
            confirmation_id=format_confirmation_ref(row.confirmation_code),
            status=effective_status,
            source_type=row.source_type,
            transport=row.transport,
            risk_reason=preview.risk_reason,
            entries_total=preview.entries_total,
            income_total=Decimal(preview.income_total or "0"),
            expense_total=Decimal(preview.expense_total or "0"),
            currency=preview.currency,
            created_at=row.created_at,
            expires_at=expires_at,
            completed_at=completed_at,
        )
