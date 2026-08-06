"""Persistent operations on ``reply_outbox`` rows (P06a Transactional Outbox).

The insert side is deliberately **not** a store method: the processor adds
``ReplyOutbox`` rows to the same session as the ledger change and commits once,
so the two are atomic. This module owns everything that happens around that
commit boundary:

* ``has_outbox`` — the recovery pre-check ("did this event already commit a
  reply intent?") used to converge a crashed event to ``succeeded`` without
  re-running business.
* ``load_by_ids`` — reload freshly committed rows from the database so the
  compatible send consumes the committed row, never an in-memory reply.
* ``mark_sent`` / ``mark_failed`` — conditional status updates. Each is guarded
  by ``status IN ('pending','sending','failed')`` so a stale send result can
  never overwrite a newer one, and a ``sent`` row is never re-sent.

A worker-leased background sender (P06b) will add ``FOR UPDATE SKIP LOCKED``
claims and lease columns on top of these primitives; P06a deliberately does not
implement any of that.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import MAX_RESULT_SUMMARY_LENGTH, safe_error_summary
from lark_ledger.models import ReplyOutbox
from lark_ledger.outbox import ReplyStatus

logger = logging.getLogger(__name__)

#: Statuses a delivery outcome may still be applied to; a terminal ``sent`` or
#: ``dead`` row is never overwritten.
_DELIVERABLE_STATUSES = (
    ReplyStatus.PENDING.value,
    ReplyStatus.SENDING.value,
    ReplyStatus.FAILED.value,
)


class ReplyOutboxStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def has_outbox(self, event_id: str) -> bool:
        """True when the event already committed at least one reply intent.

        Used as the crash-window recovery pre-check: an outbox row exists only
        if the business transaction it was written with committed, so the
        processor can skip business and let the worker converge the event to
        ``succeeded``.
        """
        async with self._factory() as session:
            row = await session.scalar(
                select(ReplyOutbox.id)
                .where(ReplyOutbox.event_id == event_id)
                .limit(1)
            )
            return row is not None

    async def load_by_ids(self, outbox_ids: list[Any]) -> list[ReplyOutbox]:
        """Reload committed rows by primary key (fresh session, committed data)."""
        if not outbox_ids:
            return []
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
            return list(rows)

    async def mark_sent(
        self, outbox_id: Any, *, result_summary: str | None, now: datetime | None = None
    ) -> bool:
        """Mark a row delivered; only applies to still-deliverable rows."""
        async with self._factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReplyOutbox)
                    .where(
                        ReplyOutbox.id == outbox_id,
                        ReplyOutbox.status.in_(_DELIVERABLE_STATUSES),
                    )
                    .values(
                        status=ReplyStatus.SENT.value,
                        sent_at=now or datetime.now(UTC),
                        last_error_code=None,
                        result_summary=result_summary,
                        updated_at=now or datetime.now(UTC),
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_failed(
        self,
        outbox_id: Any,
        *,
        error_code: str,
        summary: str,
        now: datetime | None = None,
    ) -> bool:
        """Record a failed compatible-send attempt; the row stays retryable.

        ``summary`` is a redacted, single-line, length-capped description (the
        same safety policy as ``processed_events.result_summary``). No traceback
        and no reply content is ever stored.
        """
        async with self._factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReplyOutbox)
                    .where(
                        ReplyOutbox.id == outbox_id,
                        ReplyOutbox.status.in_(_DELIVERABLE_STATUSES),
                    )
                    .values(
                        status=ReplyStatus.FAILED.value,
                        attempt_count=ReplyOutbox.attempt_count + 1,
                        last_error_code=error_code[:64],
                        result_summary=summary[:MAX_RESULT_SUMMARY_LENGTH],
                        updated_at=now or datetime.now(UTC),
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0


def record_failure_summary(exc: BaseException) -> tuple[str, str]:
    """Return ``(error_code, redacted_summary)`` for a failed send.

    Lives here so the send loop shares the exact same safety policy with every
    other failure recorder in the codebase.
    """
    return type(exc).__name__, safe_error_summary(exc)
