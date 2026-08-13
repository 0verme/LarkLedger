"""Recurring-rule generation worker (P29).

The worker turns due active rules into deterministic confirmation pendings plus
proactive Feishu reminder cards, reusing the existing lease / retry / outbox
machinery instead of a second scheduling system.

``RecurringWorkerStore.claim_and_generate`` is one transaction:

1. ``SELECT ... FOR UPDATE SKIP LOCKED`` claims due active rules
   (``status='active' AND next_occurrence <= business-today``). Concurrent
   workers can never process the same rule at once.
2. For each claimed rule the store inserts a ``RecurringOccurrence`` row. The
   unique ``(rule_id, occurrence_date)`` constraint — not an application
   ``if not exists`` — is the idempotency authority: a retried / concurrent /
   crashed run can never produce two pendings for the same period.
3. It creates the frozen confirmation pending, the occurrence link, and the
   proactive reminder card outbox row, then advances ``next_occurrence`` —
   all atomically with the single commit.

A crash before the commit rolls everything back; a crash after the commit left
``next_occurrence`` already advanced, so neither window can duplicate a pending.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.models import (
    ChannelIdentity,
    RecurringFrequency,
    RecurringOccurrence,
    RecurringOccurrenceStatus,
    RecurringRule,
    RecurringRuleStatus,
    ReplyOutbox,
)
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_direct_card_payload,
)
from lark_ledger.services.pending import (
    PendingCommandStore,
    PendingPreview,
    build_pending_preview_card,
)
from lark_ledger.services.recurring import local_business_date, next_occurrence_after
from lark_ledger.services.worker import iso_datetime, safe_owner_id

logger = logging.getLogger(__name__)

RECURRING_WORKER_TASK_NAME = "lark-ledger-recurring-worker"


@dataclass(frozen=True)
class GeneratedOccurrence:
    """One occurrence generated in a sweep (for logging / tests)."""

    rule_id: Any
    occurrence_date: date
    pending_id: Any
    confirmation_code: str


class RecurringWorkerStore:
    """Lease-guarded, idempotent due-rule generation against ``recurring_rules``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._factory = session_factory
        self._settings = settings

    async def claim_and_generate(
        self,
        owner_id: str,
        now: datetime,
        batch_size: int,
    ) -> tuple[list[GeneratedOccurrence], list[ReplyOutbox]]:
        """Lock due rules and generate exactly one pending per occurrence."""
        today = local_business_date(self._settings.timezone, now)
        pending_store = PendingCommandStore(self._factory, self._settings)
        generated: list[GeneratedOccurrence] = []
        outbox_rows: list[ReplyOutbox] = []
        async with self._factory() as session:
            rules = list(
                (
                    await session.execute(
                        select(RecurringRule)
                        .where(
                            RecurringRule.status == RecurringRuleStatus.ACTIVE.value,
                            RecurringRule.next_occurrence <= today,
                        )
                        .order_by(RecurringRule.next_occurrence, RecurringRule.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not rules:
                return [], []
            for rule in rules:
                item = await self._generate_one(
                    session=session,
                    pending_store=pending_store,
                    rule=rule,
                    owner_id=owner_id,
                )
                if item is None:
                    continue
                occurrence, rows = item
                generated.append(occurrence)
                outbox_rows.extend(rows)
            await session.commit()
        for occurrence_item in generated:
            logger.info(
                "recurring occurrence generated rule_id=%s date=%s pending=%s code=%s",
                occurrence_item.rule_id,
                occurrence_item.occurrence_date,
                occurrence_item.pending_id,
                occurrence_item.confirmation_code,
            )
        return generated, outbox_rows

    async def _generate_one(
        self,
        *,
        session: AsyncSession,
        pending_store: PendingCommandStore,
        rule: RecurringRule,
        owner_id: str,
    ) -> tuple[GeneratedOccurrence, list[ReplyOutbox]] | None:
        """Generate one occurrence + pending + reminder for ``rule``.

        Returns ``None`` when the occurrence already exists (idempotent
        re-generation guard); the caller then simply advances the schedule.
        """
        del owner_id
        occurrence_date = rule.next_occurrence
        existing = await session.scalar(
            select(RecurringOccurrence.id).where(
                RecurringOccurrence.rule_id == rule.id,
                RecurringOccurrence.occurrence_date == occurrence_date,
            )
        )
        if existing is not None:
            # Already generated / skipped: never duplicate, just advance.
            rule.next_occurrence = self._advance(rule, occurrence_date)
            return None

        user_open_id = await self._open_id_for_user(session, rule.creator_user_id)
        context = RequestContext(
            actor_user_id=rule.creator_user_id,
            ledger_id=rule.ledger_id,
            source_channel="recurring",
            external_subject_id=user_open_id,
        )
        pending = await pending_store.create_recurring_pending(
            session=session,
            context=context,
            user_open_id=user_open_id or str(rule.creator_user_id),
            rule=rule,
            occurrence_date=occurrence_date,
            now=now_utc(),
        )
        session.add(
            RecurringOccurrence(
                ledger_id=rule.ledger_id,
                rule_id=rule.id,
                occurrence_date=occurrence_date,
                status=RecurringOccurrenceStatus.PENDING.value,
                pending_id=pending.id,
            )
        )
        rows: list[ReplyOutbox] = []
        if user_open_id:
            preview = PendingPreview.from_json(pending.preview_json)
            rows.append(
                ReplyOutbox(
                    event_id=None,
                    message_id="",
                    reply_type=ReplyType.DIRECT_CARD.value,
                    sequence=0,
                    transport="feishu",
                    payload_version=OUTBOX_PAYLOAD_VERSION,
                    payload_json=build_direct_card_payload(
                        open_id=user_open_id,
                        card=build_pending_preview_card(
                            preview, timezone=self._settings.timezone
                        ),
                    ),
                    payload_blob=None,
                    status=ReplyStatus.PENDING.value,
                    attempt_count=0,
                )
            )
        rule.next_occurrence = self._advance(rule, occurrence_date)
        return (
            GeneratedOccurrence(
                rule_id=rule.id,
                occurrence_date=occurrence_date,
                pending_id=pending.id,
                confirmation_code=pending.confirmation_code,
            ),
            rows,
        )

    def _advance(self, rule: RecurringRule, occurrence_date: date) -> date:
        return next_occurrence_after(
            occurrence_date,
            frequency=RecurringFrequency(rule.frequency),
            interval=rule.interval,
            anchor_day=rule.anchor_day,
        )

    async def _open_id_for_user(
        self, session: AsyncSession, user_id: Any
    ) -> str | None:
        """Return the user's most recent Feishu open_id, if any."""
        row = await session.scalar(
            select(ChannelIdentity.external_subject_id)
            .where(
                ChannelIdentity.user_id == user_id,
                ChannelIdentity.channel == "feishu",
            )
            .order_by(ChannelIdentity.created_at.desc(), ChannelIdentity.id.desc())
            .limit(1)
        )
        return str(row) if row else None


def now_utc() -> datetime:
    return datetime.now(UTC)


class RecurringWorker:
    """Background task that repeatedly generates due recurring occurrences.

    Mirrors ``EventWorker``'s loop shape so readiness can snapshot it with the
    same ``health_snapshot`` contract. ``deliverer`` (optional) receives the
    freshly committed reminder outbox rows: when the Reply Worker is enabled it
    is the in-process wakeup signal, otherwise the compatible synchronous
    claim + deliver path runs.
    """

    def __init__(
        self,
        store: RecurringWorkerStore,
        *,
        owner_id: str,
        batch_size: int = 10,
        poll_interval_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        deliverer: Callable[[list[ReplyOutbox]], Awaitable[None]] | None = None,
    ) -> None:
        from lark_ledger.services.worker import default_clock

        self._store = store
        self._owner_id = owner_id
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or default_clock
        self._sleeper = sleeper
        self._deliverer = deliverer
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
        self._processed = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def health_snapshot(self) -> dict[str, bool | str | int | None]:
        """Return a redacted, read-only task state for readiness."""
        return {
            "started": self._started,
            "running": self.running,
            "stopping": self._stop.is_set(),
            "task_done": self._task_done,
            "task_exception": self._task_exception_code is not None,
            "last_error_code": self._task_exception_code,
            "last_sweep_at": iso_datetime(self._last_sweep_at),
            "last_success_at": iso_datetime(self._last_success_at),
            "last_error_at": iso_datetime(self._last_error_at),
            "sweeps": self._sweeps,
            "processed": self._processed,
        }

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("recurring worker already started")
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
        self._processed = 0
        self._task = asyncio.create_task(
            self._run_loop(), name=RECURRING_WORKER_TASK_NAME
        )
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
                "recurring worker task exited unexpectedly error_code=%s owner=%s",
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
                logger.exception("recurring worker task raised during shutdown")
        self._task = None

    async def _run_loop(self) -> None:
        logger.info(
            "recurring worker started owner=%s poll_interval=%.1fs batch=%d",
            safe_owner_id(self._owner_id),
            self._poll_interval_seconds,
            self._batch_size,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Connection-level failures must not kill the worker loop.
                    self._last_error_at = self._clock()
                    logger.exception("recurring worker sweep failed; will retry")
                self._last_sweep_at = self._clock()
                self._sweeps += 1
                if self._stop.is_set():
                    break
                await self._sleeper(self._poll_interval_seconds)
        finally:
            logger.info(
                "recurring worker stopped owner=%s", safe_owner_id(self._owner_id)
            )

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Claim due rules and generate occurrences; returns count generated."""
        if self._stop.is_set():
            return 0
        current = now or self._clock()
        generated, rows = await self._store.claim_and_generate(
            self._owner_id, current, self._batch_size
        )
        if rows and self._deliverer is not None:
            await self._deliverer(rows)
        if generated:
            self._last_success_at = current
            self._processed += len(generated)
        return len(generated)
