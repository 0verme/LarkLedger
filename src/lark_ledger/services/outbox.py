"""Persistent operations on ``reply_outbox`` rows (P06a + P06b).

The insert side is deliberately **not** a store method: the processor adds
``ReplyOutbox`` rows to the same session as the ledger change and commits once,
so the two are atomic. This module owns everything that happens around that
commit boundary:

* ``has_outbox`` — the recovery pre-check ("did this event already commit a
  reply intent?") used to converge a crashed event to ``succeeded`` without
  re-running business.
* ``load_by_ids`` — reload committed rows from the database (used to inspect
  freshly committed rows).
* ``claim_batch`` / ``claim_by_id`` — P06b atomic claims: one transaction does
  ``SELECT ... FOR UPDATE SKIP LOCKED``, writes ``sending`` / ``lease_owner`` /
  ``lease_expires_at``, increments ``attempt_count`` (each entry into
  ``sending`` counts one attempt), clears ``next_attempt_at``, and commits.
  ``claim_batch`` is the Reply Worker's cross-event claim and also enforces
  per-event ordering (a later sequence waits for its earlier siblings);
  ``claim_by_id`` is the single-row claim used by the compatible synchronous
  path, which already knows the rows it just committed.
* ``mark_sent`` / ``record_failure`` — lease-guarded outcomes. Each is guarded
  by ``status='sending' AND lease_owner=<owner>`` so a stale worker can never
  overwrite a newer owner's result, and a ``sent`` / ``dead`` row is never
  re-sent. ``attempt_count`` is **not** changed here (it is counted at claim).
* ``persist_file_key`` / ``persist_image_key`` — conditional updates that
  record an uploaded resource key while the worker still holds the lease, so a
  retry after a message-send failure reuses the upload instead of re-uploading.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from lark_ledger.event_payload import MAX_RESULT_SUMMARY_LENGTH, safe_error_summary
from lark_ledger.models import ReplyOutbox
from lark_ledger.outbox import ReplyStatus

logger = logging.getLogger(__name__)

#: Earlier rows in these states no longer block a later row of the same event
#: (``sent`` delivered it; ``dead`` can never succeed, so later replies are
#: allowed to proceed independently rather than being stuck forever).
_ORDERING_TERMINAL_STATUSES = (ReplyStatus.SENT.value, ReplyStatus.DEAD.value)


@dataclass(frozen=True)
class ClaimedReply:
    """A reply outbox row this worker now owns for the current attempt.

    Everything the sender needs is carried here (payload envelope, blob, and
    any already-uploaded remote keys), so delivery never re-queries the ledger,
    re-calls AI, or reopens a temporary file.
    """

    id: uuid.UUID
    event_id: str | None
    message_id: str
    reply_type: str
    sequence: int
    payload_version: int
    payload_json: dict[str, Any]
    payload_blob: bytes | None
    attempt_count: int
    remote_file_key: str | None
    remote_image_key: str | None


def _datetime_lte(a: datetime, b: datetime) -> bool:
    """``a <= b`` tolerating the SQLite naive-datetime artifact.

    SQLite drops tzinfo from stored ``timestamptz`` values, so an ORM-loaded
    ``next_attempt_at`` may be naive while ``now`` is timezone-aware. PostgreSQL
    always returns timezone-aware values (the production comparison is exact);
    the normalization only kicks in for the SQLite test mirror.
    """
    if a.tzinfo is None and b.tzinfo is not None:
        b = b.replace(tzinfo=None)
    elif b.tzinfo is None and a.tzinfo is not None:
        a = a.replace(tzinfo=None)
    return a <= b


def _claimed_from_row(row: ReplyOutbox) -> ClaimedReply:
    return ClaimedReply(
        id=row.id,
        event_id=row.event_id,
        message_id=row.message_id,
        reply_type=row.reply_type,
        sequence=row.sequence,
        payload_version=row.payload_version,
        payload_json=dict(row.payload_json or {}),
        payload_blob=row.payload_blob,
        attempt_count=row.attempt_count or 0,
        remote_file_key=row.remote_file_key,
        remote_image_key=row.remote_image_key,
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

    @staticmethod
    def _is_claimable(row: ReplyOutbox, now: datetime) -> bool:
        """P06b claim predicate for one row (no payload-integrity check)."""
        if row.status == ReplyStatus.PENDING.value:
            return True
        if (
            row.status == ReplyStatus.FAILED.value
            and row.next_attempt_at is not None
            and _datetime_lte(row.next_attempt_at, now)
        ):
            return True
        if (
            row.status == ReplyStatus.SENDING.value
            and row.lease_expires_at is not None
            and _datetime_lte(row.lease_expires_at, now)
        ):
            return True
        return False

    async def claim_batch(
        self,
        owner_id: str,
        now: datetime,
        batch_size: int,
        lease_seconds: float,
        *,
        event_id: str | None = None,
    ) -> list[ClaimedReply]:
        """Atomically claim up to ``batch_size`` deliverable outbox rows.

        One transaction: ``SELECT ... FOR UPDATE SKIP LOCKED`` picks candidates
        (``pending``, ``failed``-and-due, or ``sending``-with-expired-lease) and
        skips any row whose same-event earlier sequence is not yet ``sent`` or
        ``dead``, so a later reply never overtakes an earlier one — including
        under concurrency. The same transaction then writes ``sending``, the
        lease, and the incremented ``attempt_count`` and commits. Nothing is
        visible to other workers until the commit.

        When ``event_id`` is given the claim is restricted to that event (used
        by the synchronous compatible path). ``event_id=None`` means no filter.
        """
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._factory() as session:
            earlier = aliased(ReplyOutbox)
            blocking_earlier = (
                select(earlier.id)
                .where(
                    earlier.event_id == ReplyOutbox.event_id,
                    earlier.sequence < ReplyOutbox.sequence,
                    earlier.status.notin_(_ORDERING_TERMINAL_STATUSES),
                )
                .exists()
            )
            stmt = (
                select(ReplyOutbox)
                .where(
                    or_(
                        ReplyOutbox.status == ReplyStatus.PENDING.value,
                        and_(
                            ReplyOutbox.status == ReplyStatus.FAILED.value,
                            ReplyOutbox.next_attempt_at.is_not(None),
                            ReplyOutbox.next_attempt_at <= now,
                        ),
                        and_(
                            ReplyOutbox.status == ReplyStatus.SENDING.value,
                            ReplyOutbox.lease_expires_at.is_not(None),
                            ReplyOutbox.lease_expires_at <= now,
                        ),
                    ),
                    ~blocking_earlier,
                )
                .order_by(ReplyOutbox.created_at.asc(), ReplyOutbox.id.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            if event_id is not None:
                stmt = stmt.where(ReplyOutbox.event_id == event_id)
            rows = (await session.execute(stmt)).scalars().all()
            claimed: list[ClaimedReply] = []
            for row in rows:
                row.status = ReplyStatus.SENDING.value
                row.lease_owner = owner_id
                row.lease_expires_at = lease_expires_at
                row.attempt_count = (row.attempt_count or 0) + 1
                row.next_attempt_at = None
                row.updated_at = now
                claimed.append(_claimed_from_row(row))
            if claimed:
                await session.commit()
            return claimed

    async def claim_by_id(
        self,
        outbox_id: Any,
        owner_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> ClaimedReply | None:
        """Claim exactly one row by primary key (synchronous path).

        Locks the row with ``FOR UPDATE`` and claims it only when it is still
        claimable. Returns ``None`` when the row is missing or already in a
        terminal / in-flight state.
        """
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._factory() as session:
            row = await session.get(ReplyOutbox, outbox_id, with_for_update=True)
            if row is None or not self._is_claimable(row, now):
                return None
            row.status = ReplyStatus.SENDING.value
            row.lease_owner = owner_id
            row.lease_expires_at = lease_expires_at
            row.attempt_count = (row.attempt_count or 0) + 1
            row.next_attempt_at = None
            row.updated_at = now
            await session.commit()
            return _claimed_from_row(row)

    async def persist_file_key(
        self, outbox_id: Any, owner_id: str, *, file_key: str, now: datetime
    ) -> bool:
        """Record an uploaded Feishu ``file_key`` while still holding the lease.

        The row stays ``sending``; only the key is persisted so a later retry
        reuses it instead of re-uploading. Returns False when the lease was
        lost (the caller must then abandon this delivery).
        """
        return await self._persist_remote_key(
            outbox_id, owner_id, column=ReplyOutbox.remote_file_key, value=file_key, now=now
        )

    async def persist_image_key(
        self, outbox_id: Any, owner_id: str, *, image_key: str, now: datetime
    ) -> bool:
        """Record an uploaded Feishu ``image_key`` while still holding the lease."""
        return await self._persist_remote_key(
            outbox_id, owner_id, column=ReplyOutbox.remote_image_key, value=image_key, now=now
        )

    async def _persist_remote_key(
        self,
        outbox_id: Any,
        owner_id: str,
        *,
        column: Any,
        value: str,
        now: datetime,
    ) -> bool:
        async with self._factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReplyOutbox)
                    .where(
                        ReplyOutbox.id == outbox_id,
                        ReplyOutbox.status == ReplyStatus.SENDING.value,
                        ReplyOutbox.lease_owner == owner_id,
                    )
                    .values({column: value, ReplyOutbox.updated_at: now})
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_sent(
        self,
        outbox_id: Any,
        owner_id: str,
        *,
        now: datetime,
        result_summary: str | None = "delivered",
        remote_message_id: str | None = None,
    ) -> bool:
        """Mark a row delivered, guarded by the current lease.

        Clears the lease and records the remote ``message_id`` returned by the
        Feishu reply API. Returns False when the row no longer matches
        ``status='sending' AND lease_owner=<owner>`` (lease expired or stolen);
        the caller must then leave the row alone so the new owner's result is
        not overwritten.
        """
        async with self._factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReplyOutbox)
                    .where(
                        ReplyOutbox.id == outbox_id,
                        ReplyOutbox.status == ReplyStatus.SENDING.value,
                        ReplyOutbox.lease_owner == owner_id,
                    )
                    .values(
                        status=ReplyStatus.SENT.value,
                        sent_at=now,
                        next_attempt_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error_code=None,
                        result_summary=result_summary,
                        remote_message_id=remote_message_id,
                        updated_at=now,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def record_failure(
        self,
        outbox_id: Any,
        owner_id: str,
        *,
        status: str,
        next_attempt_at: datetime | None,
        error_code: str,
        summary: str,
        now: datetime,
    ) -> bool:
        """Record ``failed`` (retry later) or ``dead`` (terminal), lease-guarded.

        ``status`` and ``next_attempt_at`` are computed by the caller with pure
        helpers so the retry policy is unit-testable. ``attempt_count`` is not
        changed here (it is counted at claim). Lease fields are cleared on both
        outcomes so a later claim starts fresh. Returns False when the lease
        was lost and the status must not be overwritten.

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
                        ReplyOutbox.status == ReplyStatus.SENDING.value,
                        ReplyOutbox.lease_owner == owner_id,
                    )
                    .values(
                        status=status,
                        next_attempt_at=next_attempt_at,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error_code=error_code[:64],
                        result_summary=summary[:MAX_RESULT_SUMMARY_LENGTH],
                        updated_at=now,
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
