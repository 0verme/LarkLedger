"""Redacted, bounded read models for Dashboard reliability operations."""

from __future__ import annotations

from math import ceil

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import ProcessedEvent, ReplyOutbox
from lark_ledger.web_schemas import (
    AdminDeadSummary,
    AdminEvent,
    AdminEventPage,
    AdminOutbox,
    AdminOutboxPage,
)


def _mask_message_id(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return f"{value[:2]}…{value[-2:]}"
    return f"{value[:5]}…{value[-4:]}"


class WebAdminQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def events(
        self, *, status: str | None, page: int, page_size: int
    ) -> AdminEventPage:
        if status == "legacy":
            status = "legacy_succeeded"
        filters = [ProcessedEvent.status == status] if status else []
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(ProcessedEvent).where(*filters)
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(ProcessedEvent)
                .where(*filters)
                .order_by(ProcessedEvent.updated_at.desc(), ProcessedEvent.event_id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return AdminEventPage(
            items=[self._event(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            pages=ceil(total / page_size) if total else 0,
        )

    async def outbox(
        self, *, status: str | None, page: int, page_size: int
    ) -> AdminOutboxPage:
        filters = [ReplyOutbox.status == status] if status else []
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(ReplyOutbox).where(*filters)
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(ReplyOutbox)
                .where(*filters)
                .order_by(ReplyOutbox.updated_at.desc(), ReplyOutbox.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return AdminOutboxPage(
            items=[self._reply(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            pages=ceil(total / page_size) if total else 0,
        )

    async def dead_summary(self, *, limit: int = 20) -> AdminDeadSummary:
        event_count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(ProcessedEvent)
                .where(ProcessedEvent.status == "dead")
            )
            or 0
        )
        reply_count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(ReplyOutbox)
                .where(ReplyOutbox.status == "dead")
            )
            or 0
        )
        events = (
            await self._session.scalars(
                select(ProcessedEvent)
                .where(ProcessedEvent.status == "dead")
                .order_by(ProcessedEvent.updated_at.desc())
                .limit(limit)
            )
        ).all()
        replies = (
            await self._session.scalars(
                select(ReplyOutbox)
                .where(ReplyOutbox.status == "dead")
                .order_by(ReplyOutbox.updated_at.desc())
                .limit(limit)
            )
        ).all()
        return AdminDeadSummary(
            event_count=event_count,
            reply_count=reply_count,
            latest_events=[self._event(row) for row in events],
            latest_replies=[self._reply(row) for row in replies],
        )

    @staticmethod
    def _event(row: ProcessedEvent) -> AdminEvent:
        return AdminEvent(
            event_id=row.event_id,
            source_message_id=_mask_message_id(row.source_message_id),
            status=row.status,
            attempt_count=row.attempt_count,
            transport=row.transport,
            received_at=row.received_at,
            processed_at=row.processed_at,
            last_error_code=row.last_error_code,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _reply(row: ReplyOutbox) -> AdminOutbox:
        return AdminOutbox(
            id=str(row.id),
            event_id=row.event_id,
            reply_type=row.reply_type,
            sequence=row.sequence,
            status=row.status,
            attempt_count=row.attempt_count,
            created_at=row.created_at,
            sent_at=row.sent_at,
            last_error_code=row.last_error_code,
        )
