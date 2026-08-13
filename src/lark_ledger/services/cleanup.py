"""Bounded retention cleanup for terminal event and reply-delivery rows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import EventProcessStatus
from lark_ledger.models import (
    ClientIdempotencyRecord,
    DashboardSession,
    PendingCommand,
    PendingStatus,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.outbox import ReplyStatus

logger = logging.getLogger(__name__)

CLEANUP_TASK_NAME = "lark-ledger-cleanup-worker"


def default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RetentionPolicy:
    event_succeeded_days: int = 30
    event_dead_days: int = 90
    outbox_sent_days: int = 30
    outbox_dead_days: int = 90
    # Terminal pending confirmations (P07): executed / cancelled / expired /
    # failed rows are deleted after this many days.
    pending_retention_days: int = 7
    # P37: soft-revoked / expired human sessions are physically removed after
    # this many days. Until then they stay queryable for the session UI and
    # incident analysis — never delete them immediately on revoke.
    session_retention_days: int = 30

    def __post_init__(self) -> None:
        values = (
            self.event_succeeded_days,
            self.event_dead_days,
            self.outbox_sent_days,
            self.outbox_dead_days,
            self.pending_retention_days,
            self.session_retention_days,
        )
        if any(value < 1 for value in values):
            raise ValueError("retention windows must be at least one day")


@dataclass(frozen=True)
class CleanupResult:
    outbox_sent: int = 0
    outbox_dead: int = 0
    event_succeeded: int = 0
    event_legacy_succeeded: int = 0
    event_dead: int = 0
    pending_expired: int = 0
    pending_deleted: int = 0
    client_idempotency_deleted: int = 0
    sessions_deleted: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.outbox_sent,
                self.outbox_dead,
                self.event_succeeded,
                self.event_legacy_succeeded,
                self.event_dead,
                self.pending_expired,
                self.pending_deleted,
                self.client_idempotency_deleted,
                self.sessions_deleted,
            )
        )


class CleanupStore:
    """Delete one lock-safe terminal batch in a short transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def delete_outbox_batch(
        self,
        *,
        status: ReplyStatus,
        cutoff: datetime,
        now: datetime,
        batch_size: int,
    ) -> int:
        if status not in {ReplyStatus.SENT, ReplyStatus.DEAD}:
            raise ValueError("only terminal outbox statuses may be cleaned")
        timestamp = ReplyOutbox.sent_at if status is ReplyStatus.SENT else ReplyOutbox.updated_at
        async with self._factory() as session:
            ids = list(
                (
                    await session.scalars(
                        select(ReplyOutbox.id)
                        .where(
                            ReplyOutbox.status == status.value,
                            timestamp.is_not(None),
                            timestamp <= cutoff,
                            or_(
                                ReplyOutbox.lease_expires_at.is_(None),
                                ReplyOutbox.lease_expires_at <= now,
                            ),
                        )
                        .order_by(timestamp, ReplyOutbox.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not ids:
                return 0
            await session.execute(delete(ReplyOutbox).where(ReplyOutbox.id.in_(ids)))
            await session.commit()
            return len(ids)

    async def delete_event_batch(
        self,
        *,
        status: EventProcessStatus,
        cutoff: datetime,
        now: datetime,
        batch_size: int,
    ) -> int:
        if status not in {
            EventProcessStatus.SUCCEEDED,
            EventProcessStatus.LEGACY_SUCCEEDED,
            EventProcessStatus.DEAD,
        }:
            raise ValueError("only terminal event statuses may be cleaned")
        timestamp = (
            ProcessedEvent.updated_at
            if status is EventProcessStatus.DEAD
            else ProcessedEvent.processed_at
        )
        has_outbox = exists(
            select(ReplyOutbox.id).where(ReplyOutbox.event_id == ProcessedEvent.event_id)
        )
        async with self._factory() as session:
            event_ids = list(
                (
                    await session.scalars(
                        select(ProcessedEvent.event_id)
                        .where(
                            ProcessedEvent.status == status.value,
                            timestamp <= cutoff,
                            or_(
                                ProcessedEvent.lease_expires_at.is_(None),
                                ProcessedEvent.lease_expires_at <= now,
                            ),
                            ~has_outbox,
                        )
                        .order_by(timestamp, ProcessedEvent.event_id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not event_ids:
                return 0
            await session.execute(
                delete(ProcessedEvent).where(ProcessedEvent.event_id.in_(event_ids))
            )
            await session.commit()
            return len(event_ids)

    async def expire_pending_batch(
        self,
        *,
        cutoff: datetime,
        now: datetime,
        batch_size: int,
    ) -> int:
        """Mark pending confirmations past their expiry as expired (idempotent).

        Only touches ``pending`` rows; executed / cancelled / failed rows are
        never re-expired, and non-pending rows are never selected.
        """
        async with self._factory() as session:
            ids = list(
                (
                    await session.scalars(
                        select(PendingCommand.id)
                        .where(
                            PendingCommand.status == PendingStatus.PENDING.value,
                            PendingCommand.expires_at.is_not(None),
                            PendingCommand.expires_at <= cutoff,
                        )
                        .order_by(PendingCommand.expires_at, PendingCommand.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not ids:
                return 0
            await session.execute(
                update(PendingCommand)
                .where(PendingCommand.id.in_(ids))
                .values(status=PendingStatus.EXPIRED.value, updated_at=now)
            )
            await session.commit()
            return len(ids)

    async def delete_pending_terminal_batch(
        self,
        *,
        cutoff: datetime,
        now: datetime,
        batch_size: int,
    ) -> int:
        """Delete terminal pending rows (executed/cancelled/expired/failed) that
        are past the retention cutoff. Open ``pending`` rows are never selected."""
        terminal = (
            PendingStatus.EXECUTED.value,
            PendingStatus.CANCELLED.value,
            PendingStatus.EXPIRED.value,
            PendingStatus.FAILED.value,
        )
        async with self._factory() as session:
            ids = list(
                (
                    await session.scalars(
                        select(PendingCommand.id)
                        .where(
                            PendingCommand.status.in_(terminal),
                            PendingCommand.updated_at <= cutoff,
                        )
                        .order_by(PendingCommand.updated_at, PendingCommand.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not ids:
                return 0
            await session.execute(
                delete(PendingCommand).where(PendingCommand.id.in_(ids))
            )
            await session.commit()
            return len(ids)

    async def delete_client_idempotency_batch(
        self, *, cutoff: datetime, batch_size: int
    ) -> int:
        """Delete only expired client snapshots; active keys are never selected."""
        async with self._factory() as session:
            ids = list(
                (
                    await session.scalars(
                        select(ClientIdempotencyRecord.id)
                        .where(ClientIdempotencyRecord.expires_at <= cutoff)
                        .order_by(
                            ClientIdempotencyRecord.expires_at,
                            ClientIdempotencyRecord.id,
                        )
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not ids:
                return 0
            await session.execute(
                delete(ClientIdempotencyRecord).where(
                    ClientIdempotencyRecord.id.in_(ids)
                )
            )
            await session.commit()
            return len(ids)

    async def delete_session_batch(
        self, *, cutoff: datetime, now: datetime, batch_size: int
    ) -> int:
        """Delete only revoked-or-expired human sessions past the retention
        cutoff (P37 §21). Active sessions are never selected."""
        async with self._factory() as session:
            ids = list(
                (
                    await session.scalars(
                        select(DashboardSession.id)
                        .where(
                            or_(
                                DashboardSession.revoked_at.is_not(None),
                                DashboardSession.expires_at <= now,
                            ),
                            DashboardSession.created_at <= cutoff,
                        )
                        .order_by(DashboardSession.created_at, DashboardSession.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not ids:
                return 0
            await session.execute(
                delete(DashboardSession).where(DashboardSession.id.in_(ids))
            )
            await session.commit()
            return len(ids)


class CleanupService:
    """Apply terminal retention in outbox-before-event order."""

    def __init__(
        self,
        store: CleanupStore,
        policy: RetentionPolicy,
        *,
        batch_size: int = 500,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._store = store
        self._policy = policy
        self._batch_size = batch_size

    async def run_once(self, *, now: datetime | None = None) -> CleanupResult:
        current = now or default_clock()
        if current.tzinfo is None:
            raise ValueError("cleanup clock must be timezone-aware")
        cutoffs = {
            "outbox_sent": current - timedelta(days=self._policy.outbox_sent_days),
            "outbox_dead": current - timedelta(days=self._policy.outbox_dead_days),
            "event_succeeded": current - timedelta(days=self._policy.event_succeeded_days),
            "event_dead": current - timedelta(days=self._policy.event_dead_days),
            "pending_expired": current,
            "pending_deleted": current - timedelta(days=self._policy.pending_retention_days),
            "client_idempotency_deleted": current,
            "sessions_deleted": current - timedelta(days=self._policy.session_retention_days),
        }
        counts: dict[str, int] = {}
        counts["outbox_sent"] = await self._timed_delete(
            "outbox_sent",
            cutoffs["outbox_sent"],
            self._store.delete_outbox_batch(
                status=ReplyStatus.SENT,
                cutoff=cutoffs["outbox_sent"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["outbox_dead"] = await self._timed_delete(
            "outbox_dead",
            cutoffs["outbox_dead"],
            self._store.delete_outbox_batch(
                status=ReplyStatus.DEAD,
                cutoff=cutoffs["outbox_dead"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["event_succeeded"] = await self._timed_delete(
            "event_succeeded",
            cutoffs["event_succeeded"],
            self._store.delete_event_batch(
                status=EventProcessStatus.SUCCEEDED,
                cutoff=cutoffs["event_succeeded"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["event_legacy_succeeded"] = await self._timed_delete(
            "event_legacy_succeeded",
            cutoffs["event_succeeded"],
            self._store.delete_event_batch(
                status=EventProcessStatus.LEGACY_SUCCEEDED,
                cutoff=cutoffs["event_succeeded"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["event_dead"] = await self._timed_delete(
            "event_dead",
            cutoffs["event_dead"],
            self._store.delete_event_batch(
                status=EventProcessStatus.DEAD,
                cutoff=cutoffs["event_dead"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["pending_expired"] = await self._timed_delete(
            "pending_expired",
            cutoffs["pending_expired"],
            self._store.expire_pending_batch(
                cutoff=cutoffs["pending_expired"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["pending_deleted"] = await self._timed_delete(
            "pending_deleted",
            cutoffs["pending_deleted"],
            self._store.delete_pending_terminal_batch(
                cutoff=cutoffs["pending_deleted"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        counts["client_idempotency_deleted"] = await self._timed_delete(
            "client_idempotency_deleted",
            cutoffs["client_idempotency_deleted"],
            self._store.delete_client_idempotency_batch(
                cutoff=cutoffs["client_idempotency_deleted"],
                batch_size=self._batch_size,
            ),
        )
        counts["sessions_deleted"] = await self._timed_delete(
            "sessions_deleted",
            cutoffs["sessions_deleted"],
            self._store.delete_session_batch(
                cutoff=cutoffs["sessions_deleted"],
                now=current,
                batch_size=self._batch_size,
            ),
        )
        return CleanupResult(**counts)

    @staticmethod
    async def _timed_delete(kind: str, cutoff: datetime, operation: Awaitable[int]) -> int:
        started = monotonic()
        try:
            deleted = await operation
        except Exception as exc:
            logger.warning(
                "terminal cleanup batch failed kind=%s cutoff=%s error_code=%s",
                kind,
                cutoff.isoformat(),
                type(exc).__name__,
            )
            raise
        logger.info(
            "terminal cleanup batch kind=%s cutoff=%s deleted=%d elapsed_ms=%d",
            kind,
            cutoff.isoformat(),
            deleted,
            round((monotonic() - started) * 1000),
        )
        return deleted


class CleanupWorker:
    """Periodic best-effort cleanup; failure never stops core delivery."""

    def __init__(
        self,
        service: CleanupService,
        *,
        interval_seconds: float = 3600.0,
        clock: Callable[[], datetime] = default_clock,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._service = service
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._task_done = False
        self._task_exception_code: str | None = None
        # P42 worker observability: in-process loop heartbeat (same contract as
        # ``EventWorker``) so readiness and /ops/status can report staleness.
        self._last_sweep_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._sweeps = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def health_snapshot(self) -> dict[str, bool | str | int | None]:
        return {
            "started": self._started,
            "running": self.running,
            "stopping": self._stop.is_set(),
            "task_done": self._task_done,
            "task_exception": self._task_exception_code is not None,
            "last_error_code": self._task_exception_code,
            "last_sweep_at": self._last_sweep_at.isoformat()
            if self._last_sweep_at is not None
            else None,
            "last_success_at": self._last_success_at.isoformat()
            if self._last_success_at is not None
            else None,
            "last_error_at": self._last_error_at.isoformat()
            if self._last_error_at is not None
            else None,
            "sweeps": self._sweeps,
        }

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("cleanup worker already started")
        self._stop.clear()
        self._started = True
        self._task_done = False
        self._task_exception_code = None
        # Restart resets the heartbeat so stale timestamps from a previous run
        # can never make the new run look wedged.
        self._last_sweep_at = None
        self._last_success_at = None
        self._last_error_at = None
        self._sweeps = 0
        self._task = asyncio.create_task(self._run_loop(), name=CLEANUP_TASK_NAME)
        self._task.add_done_callback(self._consume_task_result)

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        self._task_done = True
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._task_exception_code = type(exc).__name__
            self._last_error_at = self._clock()
            logger.error(
                "cleanup worker task exited unexpectedly error_code=%s",
                self._task_exception_code,
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
                logger.exception("cleanup worker task raised during shutdown")
        self._task = None

    async def _run_loop(self) -> None:
        logger.info(
            "cleanup worker started interval_seconds=%.0f",
            self._interval_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._last_error_at = self._clock()
                    logger.warning("cleanup worker sweep failed; will retry")
                self._last_sweep_at = self._clock()
                self._sweeps += 1
                if self._stop.is_set():
                    break
                await self._sleeper(self._interval_seconds)
        finally:
            logger.info("cleanup worker stopped")

    async def run_once(self, *, now: datetime | None = None) -> CleanupResult:
        if self._stop.is_set():
            return CleanupResult()
        result = await self._service.run_once(now=now or self._clock())
        self._last_success_at = self._clock()
        return result
