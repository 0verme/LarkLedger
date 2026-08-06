"""PostgreSQL-driven event worker with lease, retry, and dead-letter handling.

P05b: a background ``EventWorker`` claims ``processed_events`` rows with
``SELECT ... FOR UPDATE SKIP LOCKED``, runs the ``MessageProcessor`` on the
payload reloaded from the database, and records lease-guarded outcomes:

* ``succeeded`` — the processor returned normally and this worker still holds
  the lease.
* ``failed`` — a retryable error; ``next_attempt_at`` is scheduled with
  exponential backoff so the event is picked up again later.
* ``dead`` — a permanent error (payload / contract / duplicate) or the attempt
  budget is exhausted; the event is never claimed again.

The database is the only queue and coordination store. Concurrent workers in
other processes are safe because a claim is a single transaction that locks
rows with ``FOR UPDATE SKIP LOCKED`` and writes ``processing``,
``lease_owner``, and ``lease_expires_at`` before committing. A crashed worker
leaves its rows ``processing`` with an expiring lease, so another worker can
take them over. Status updates are guarded by ``status='processing' AND
lease_owner=<owner>`` so a stale worker can never overwrite a new owner's
state.

Since P06a, ``succeeded`` means "business handled and its reply intents durably
written to ``reply_outbox``" — Feishu delivery state lives on the outbox rows,
not the event. The processor's outbox pre-check lets a crashed event (committed
business + outbox, then a lost status update) converge to ``succeeded`` on
re-claim without re-running business. This module does **not** implement reply
compensation, an outbox background worker, or human replay; those belong to
P06b. Existing ledger idempotency keys (the unique
``(source_message_id, source_item_index)`` constraint) remain the fallback
guard against duplicate entries.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import sqlalchemy.exc
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import (
    MAX_RESULT_SUMMARY_LENGTH,
    EventPayloadError,
    EventProcessStatus,
    business_event_from_payload,
    parse_stored_payload,
    safe_error_summary,
)
from lark_ledger.models import ProcessedEvent
from lark_ledger.services.events import EventProcessor

logger = logging.getLogger(__name__)


def default_clock() -> datetime:
    """Injected clock default: timezone-aware UTC now."""
    return datetime.now(UTC)


def generate_owner_id() -> str:
    """Stable-per-run worker identity: ``hostname:pid:nonce``."""
    return f"{socket.gethostname() or 'host'}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def safe_owner_id(owner_id: str) -> str:
    """A shortened owner label for logs (hostname:pid, no random nonce)."""
    parts = owner_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return owner_id[:20]


def compute_retry_delay_seconds(
    attempt_count: int, *, base_seconds: float, max_seconds: float
) -> float:
    """Exponential backoff: ``base * 2 ** (attempt - 1)``, capped at ``max_seconds``."""
    attempt = max(attempt_count, 1)
    delay = base_seconds * (2.0 ** (attempt - 1))
    if delay > max_seconds:
        return max_seconds
    return delay


def schedule_next_attempt(
    now: datetime,
    attempt_count: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter: Callable[[float], float] | None = None,
) -> datetime:
    """Return the timezone-aware time of the next retry for the failed attempt."""
    delay = compute_retry_delay_seconds(
        attempt_count, base_seconds=base_seconds, max_seconds=max_seconds
    )
    if jitter is not None:
        delay = jitter(delay)
    return now + timedelta(seconds=delay)


def failure_status(attempt_count: int, *, max_attempts: int, permanent: bool) -> str:
    """Decide ``failed`` (retry later) vs ``dead`` (terminal) after a failure.

    The attempt budget includes the first attempt: when ``attempt_count`` has
    reached ``max_attempts``, another retry would exceed the budget, so the
    event is dead.
    """
    if permanent or attempt_count >= max_attempts:
        return EventProcessStatus.DEAD.value
    return EventProcessStatus.FAILED.value


#: HTTP codes that usually mean "try again later"; other 4xx are permanent.
_TRANSIENT_HTTP_CODES: frozenset[int] = frozenset({408, 429})


def is_permanent_error(exc: BaseException) -> bool:
    """Conservative, explainable error classification.

    Permanent classes are small and explicit; anything not matched here
    defaults to **retryable** so transient network / AI / Feishu / database
    failures are retried rather than dropped:

    * ``EventPayloadError`` — payload cannot be parsed or is not replayable.
    * ``ValueError`` / ``TypeError`` — a business contract or field error that
      the same input would reproduce forever.
    * ``IntegrityError`` — a duplicate / constraint violation will not resolve
      on retry (and is the ledger's own double-entry guard).
    * Non-408/429 4xx HTTP — explicit client / auth errors (e.g. invalid AI key,
      missing permission) are permanent.

    Unknown errors are retried conservatively up to ``event_max_attempts`` and
    then moved to ``dead``, so a misclassified transient failure is bounded
    rather than retried forever.
    """
    if isinstance(exc, EventPayloadError):
        return True
    if isinstance(exc, (ValueError, TypeError)):
        return True
    if isinstance(exc, sqlalchemy.exc.IntegrityError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code not in _TRANSIENT_HTTP_CODES
    return False


def _default_jitter(delay: float) -> float:
    """Small randomized spread (10%) so retries do not synchronize across workers."""
    return delay * random.uniform(0.9, 1.1)


@dataclass(frozen=True)
class ClaimedEvent:
    """An event this worker owns for the current attempt."""

    event_id: str
    attempt_count: int


class EventWorkerStore:
    """Lease-guarded claim / complete / fail operations on ``processed_events``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def claim_batch(
        self,
        owner_id: str,
        now: datetime,
        batch_size: int,
        lease_seconds: float,
    ) -> list[ClaimedEvent]:
        """Atomically claim up to ``batch_size`` events and mark them processing.

        One transaction: ``SELECT ... FOR UPDATE SKIP LOCKED`` picks candidate
        rows (received, failed-and-due, or processing-with-expired-lease, with a
        non-NULL payload), then the same transaction writes ``processing``,
        ``lease_owner``, ``lease_expires_at``, increments ``attempt_count``, and
        commits. Concurrent workers cannot claim the same row, and nothing is
        visible to other workers until the commit.
        """
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._factory() as session:
            stmt = (
                select(ProcessedEvent.event_id)
                .where(
                    or_(
                        ProcessedEvent.status == EventProcessStatus.RECEIVED.value,
                        and_(
                            ProcessedEvent.status == EventProcessStatus.FAILED.value,
                            ProcessedEvent.next_attempt_at.is_not(None),
                            ProcessedEvent.next_attempt_at <= now,
                        ),
                        and_(
                            ProcessedEvent.status == EventProcessStatus.PROCESSING.value,
                            ProcessedEvent.lease_expires_at.is_not(None),
                            ProcessedEvent.lease_expires_at <= now,
                        ),
                    ),
                    ProcessedEvent.payload_json.is_not(None),
                )
                .order_by(
                    ProcessedEvent.received_at.asc().nulls_last(),
                    ProcessedEvent.event_id.asc(),
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            event_ids = (await session.execute(stmt)).scalars().all()
            claimed: list[ClaimedEvent] = []
            for event_id in event_ids:
                row = await session.get(ProcessedEvent, event_id)
                if row is None:
                    continue
                row.status = EventProcessStatus.PROCESSING.value
                row.lease_owner = owner_id
                row.lease_expires_at = lease_expires_at
                row.attempt_count = (row.attempt_count or 0) + 1
                row.next_attempt_at = None
                row.updated_at = now
                claimed.append(
                    ClaimedEvent(event_id=str(event_id), attempt_count=row.attempt_count)
                )
            if claimed:
                await session.commit()
            return claimed

    async def load_payload(self, event_id: str) -> dict[str, Any]:
        """Reload the business event from the committed payload (DB round-trip)."""
        async with self._factory() as session:
            row = await session.get(ProcessedEvent, event_id)
            if row is None:
                raise EventPayloadError(f"claimed event missing from database: {event_id}")
            if row.payload_json is None:
                raise EventPayloadError(
                    f"event {event_id} has no payload and is not replayable"
                )
            parsed = parse_stored_payload(row.payload_json)
            return business_event_from_payload(parsed)

    async def complete(self, event_id: str, owner_id: str, now: datetime) -> bool:
        """Mark an event succeeded, but only while this worker still holds the lease.

        Returns False when the row no longer matches ``status='processing'`` and
        ``lease_owner=<owner_id>`` (lease expired or stolen); the caller must
        then leave the row alone so the new owner's result is not overwritten.
        """
        async with self._factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ProcessedEvent)
                    .where(
                        ProcessedEvent.event_id == event_id,
                        ProcessedEvent.status == EventProcessStatus.PROCESSING.value,
                        ProcessedEvent.lease_owner == owner_id,
                    )
                    .values(
                        status=EventProcessStatus.SUCCEEDED.value,
                        next_attempt_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error_code=None,
                        result_summary=None,
                        updated_at=now,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def record_failure(
        self,
        event_id: str,
        owner_id: str,
        *,
        status: str,
        next_attempt_at: datetime | None,
        error_code: str,
        summary: str,
        now: datetime,
    ) -> bool:
        """Record ``failed`` or ``dead`` for an event, guarded by the lease.

        ``status`` and ``next_attempt_at`` are computed by the caller with the
        pure helpers so the retry policy is unit-testable. Lease fields are
        cleared on both outcomes so a later claim starts fresh. Returns False
        when the lease was lost and the status must not be overwritten.
        """
        async with self._factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ProcessedEvent)
                    .where(
                        ProcessedEvent.event_id == event_id,
                        ProcessedEvent.status == EventProcessStatus.PROCESSING.value,
                        ProcessedEvent.lease_owner == owner_id,
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


class EventWorker:
    """Background task that repeatedly claims and processes events.

    ``start`` creates the loop task; ``stop`` requests a stop, cancels the task,
    and waits for it to finish so no dangling task survives shutdown. Tests can
    inject ``clock``, ``sleeper``, ``owner_id``, the store, and the processor to
    avoid real time, real sleep, and real network.
    """

    def __init__(
        self,
        store: EventWorkerStore,
        processor: EventProcessor,
        *,
        owner_id: str,
        batch_size: int = 10,
        poll_interval_seconds: float = 1.0,
        max_attempts: int = 3,
        lease_seconds: float = 300.0,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 3600.0,
        clock: Callable[[], datetime] = default_clock,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] | None = _default_jitter,
    ) -> None:
        self._store = store
        self._processor = processor
        self._owner_id = owner_id
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._jitter = jitter
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._task_done = False
        self._task_exception_code: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def health_snapshot(self) -> dict[str, bool | str | None]:
        """Return a redacted, read-only task state for readiness."""
        return {
            "started": self._started,
            "running": self.running,
            "stopping": self._stop.is_set(),
            "task_done": self._task_done,
            "task_exception": self._task_exception_code is not None,
            "last_error_code": self._task_exception_code,
        }

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("event worker already started")
        self._stop.clear()
        self._started = True
        self._task_done = False
        self._task_exception_code = None
        self._task = asyncio.create_task(self._run_loop(), name="lark-ledger-event-worker")
        self._task.add_done_callback(self._consume_task_result)

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        self._task_done = True
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._task_exception_code = type(exc).__name__
            logger.error(
                "event worker task exited unexpectedly error_code=%s owner=%s",
                self._task_exception_code,
                safe_owner_id(self._owner_id),
            )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("event worker task raised during shutdown")
        self._task = None

    async def _run_loop(self) -> None:
        logger.info(
            "event worker started owner=%s poll_interval=%.1fs batch=%d "
            "max_attempts=%d lease=%.0fs retry_base=%.1fs retry_max=%.0fs",
            safe_owner_id(self._owner_id),
            self._poll_interval_seconds,
            self._batch_size,
            self._max_attempts,
            self._lease_seconds,
            self._retry_base_seconds,
            self._retry_max_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Connection-level failures must not kill the worker loop.
                    logger.exception("event worker sweep failed; will retry")
                if self._stop.is_set():
                    break
                await self._sleeper(self._poll_interval_seconds)
        finally:
            logger.info("event worker stopped owner=%s", safe_owner_id(self._owner_id))

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Claim and process one sweep; returns the number of claimed events."""
        if self._stop.is_set():
            return 0
        current = now or self._clock()
        claimed = await self._store.claim_batch(
            self._owner_id, current, self._batch_size, self._lease_seconds
        )
        for item in claimed:
            if self._stop.is_set():
                break
            await self._process_claimed(item, current)
        return len(claimed)

    async def _process_claimed(self, item: ClaimedEvent, now: datetime) -> None:
        event_id = item.event_id
        attempt = item.attempt_count
        try:
            business_event = await self._store.load_payload(event_id)
            await self._processor.process(business_event)
        except asyncio.CancelledError:
            # Leave the row processing+leased; another worker reclaims it after
            # the lease expires. This is the crash-recovery path.
            raise
        except Exception as exc:
            await self._record_failure(item, now, exc)
            return
        recorded = await self._store.complete(event_id, self._owner_id, now)
        if recorded:
            logger.info("event succeeded event_id=%s attempt=%d", event_id, attempt)
        else:
            logger.warning(
                "event lease lost after processing; status not overwritten "
                "event_id=%s owner=%s",
                event_id,
                safe_owner_id(self._owner_id),
            )

    async def _record_failure(
        self, item: ClaimedEvent, now: datetime, exc: BaseException
    ) -> None:
        event_id = item.event_id
        attempt = item.attempt_count
        permanent = is_permanent_error(exc)
        status = failure_status(attempt, max_attempts=self._max_attempts, permanent=permanent)
        next_attempt_at: datetime | None = None
        if status == EventProcessStatus.FAILED.value:
            next_attempt_at = schedule_next_attempt(
                now,
                attempt,
                base_seconds=self._retry_base_seconds,
                max_seconds=self._retry_max_seconds,
                jitter=self._jitter,
            )
        error_code = type(exc).__name__
        recorded = await self._store.record_failure(
            event_id,
            self._owner_id,
            status=status,
            next_attempt_at=next_attempt_at,
            error_code=error_code,
            summary=safe_error_summary(exc),
            now=now,
        )
        if not recorded:
            logger.warning(
                "event lease lost while recording failure; status not overwritten "
                "event_id=%s owner=%s",
                event_id,
                safe_owner_id(self._owner_id),
            )
            return
        if status == EventProcessStatus.DEAD.value:
            logger.warning(
                "event moved to dead event_id=%s attempt=%d error_code=%s",
                event_id,
                attempt,
                error_code,
            )
        else:
            logger.warning(
                "event failed and will retry event_id=%s attempt=%d error_code=%s "
                "next_attempt_at=%s",
                event_id,
                attempt,
                error_code,
                next_attempt_at,
            )
