"""Internal result-replay capability for committed reply outbox rows (P06b).

``OutboxReplayService`` replays **only already-persisted outbox intents**: it
moves ``failed`` / ``dead`` rows back to ``pending`` so the Reply Worker (or the
compatible synchronous path) delivers the exact same payload again. It never
re-calls AI, never re-runs a business command, never re-queries the ledger to
regenerate a file, and never re-renders a report — everything the sender needs
already lives on the row (``payload_json`` / ``payload_blob`` plus any
already-uploaded remote keys).

This is deliberately **not** a user-visible command yet: there is no natural-
language entry point and no unauthenticated HTTP endpoint. The service is an
internal, testable capability; a safe user-facing replay command is left for a
later work package.

Transport re-delivery of the same ``event_id`` never goes through here — the
``MessageProcessor`` outbox pre-check already skips business for an event that
has outbox rows, and ``sent`` rows are never replayed or re-sent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.models import ReplyOutbox
from lark_ledger.outbox import ReplyStatus

#: Statuses an operator may reset to ``pending`` for delivery again.
_REPLAYABLE_STATUSES = (ReplyStatus.FAILED.value, ReplyStatus.DEAD.value)


@dataclass(frozen=True)
class OutboxStatusView:
    """Read-only view of one outbox row's delivery state."""

    outbox_id: uuid.UUID
    event_id: str | None
    reply_type: str
    sequence: int
    status: str
    attempt_count: int
    sent_at: datetime | None
    last_error_code: str | None
    result_summary: str | None
    remote_message_id: str | None


@dataclass(frozen=True)
class ReplayResult:
    """Counts from a replay request (auditable, no content is echoed)."""

    reset: int
    skipped: int
    not_found: int

    def summary(self) -> str:
        return (
            f"replay reset={self.reset} skipped={self.skipped} not_found={self.not_found}"
        )


class OutboxReplayService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def status_by_event(self, event_id: str) -> list[OutboxStatusView]:
        """Return delivery state for every outbox row of one event."""
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ReplyOutbox)
                        .where(ReplyOutbox.event_id == event_id)
                        .order_by(ReplyOutbox.sequence.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [self._view(row) for row in rows]

    async def replay_ids(
        self, outbox_ids: list[Any], *, now: datetime | None = None
    ) -> ReplayResult:
        """Reset the given ``failed`` / ``dead`` rows to ``pending``.

        Rows already in another state (``pending`` / ``sending`` / ``sent``) and
        missing rows are skipped and counted. The rows are only re-queued; the
        Reply Worker then delivers them through the normal claim/lease path.
        """
        if not outbox_ids:
            return ReplayResult(reset=0, skipped=0, not_found=0)
        current = now or datetime.now(UTC)
        reset = 0
        skipped = 0
        not_found = 0
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ReplyOutbox).where(ReplyOutbox.id.in_(outbox_ids))
                    )
                )
                .scalars()
                .all()
            )
            by_id = {row.id: row for row in rows}
            for outbox_id in outbox_ids:
                row = by_id.get(outbox_id)
                if row is None:
                    not_found += 1
                    continue
                if row.status not in _REPLAYABLE_STATUSES:
                    skipped += 1
                    continue
                row.status = ReplyStatus.PENDING.value
                row.next_attempt_at = None
                row.lease_owner = None
                row.lease_expires_at = None
                row.last_error_code = None
                row.result_summary = None
                row.updated_at = current
                reset += 1
            await session.commit()
        return ReplayResult(reset=reset, skipped=skipped, not_found=not_found)

    async def replay_event(self, event_id: str, *, now: datetime | None = None) -> ReplayResult:
        """Reset every ``failed`` / ``dead`` outbox row of one event to ``pending``."""
        current = now or datetime.now(UTC)
        reset = 0
        skipped = 0
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ReplyOutbox).where(ReplyOutbox.event_id == event_id)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if row.status not in _REPLAYABLE_STATUSES:
                    skipped += 1
                    continue
                row.status = ReplyStatus.PENDING.value
                row.next_attempt_at = None
                row.lease_owner = None
                row.lease_expires_at = None
                row.last_error_code = None
                row.result_summary = None
                row.updated_at = current
                reset += 1
            await session.commit()
        return ReplayResult(reset=reset, skipped=skipped, not_found=0)

    @staticmethod
    def _view(row: ReplyOutbox) -> OutboxStatusView:
        return OutboxStatusView(
            outbox_id=row.id,
            event_id=row.event_id,
            reply_type=row.reply_type,
            sequence=row.sequence,
            status=row.status,
            attempt_count=row.attempt_count,
            sent_at=row.sent_at,
            last_error_code=row.last_error_code,
            result_summary=row.result_summary,
            remote_message_id=row.remote_message_id,
        )
