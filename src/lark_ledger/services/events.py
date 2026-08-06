import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import (
    MAX_RESULT_SUMMARY_LENGTH,
    REPLAY_SAFETY_VERSION,
    EventPayloadError,
    EventProcessStatus,
    build_stored_payload,
    business_event_from_payload,
    message_id_from_event,
    parse_stored_payload,
    safe_error_summary,
    serialize_payload,
    user_open_id_from_event,
)
from lark_ledger.models import ProcessedEvent

logger = logging.getLogger(__name__)


class EventProcessor(Protocol):
    async def process(self, event: dict[str, Any]) -> None: ...


class EventService:
    """Shared idempotent entry point for all Feishu event transports.

    Two execution modes, selected once at construction from
    ``worker_enabled``:

    * **Worker mode (``worker_enabled=True``, production default):** entry
      points only **claim** — insert ``processed_events`` with a versioned,
      normalized payload and commit. A background ``EventWorker`` (P05b) then
      claims the row, reloads the payload from the database, and processes it
      with retry / lease / dead-letter handling. Webhook responses and
      WebSocket callbacks never wait for AI or ledger work.
    * **Sync mode (``worker_enabled=False``):** the legacy claim-first path
      runs the processor synchronously right after the claim, exactly as in
      v0.2.0. This is retained for testing and for deployments that disable the
      worker. T2 failures are recorded as ``failed`` with a safe
      ``result_summary`` and are **not** retried.

    Transaction boundary (v0.2.0 / P00, extended by P06a) in both modes:

    * **T1 — claim:** insert with payload and commit. Primary-key conflict means
      the event was already claimed (still returns the dedup result immediately).
    * **T2 — process:** run the processor on a payload reloaded from the
      database (round-trip contract), owned by the worker in worker mode. Since
      P06a the processor commits business + ``reply_outbox`` intents atomically
      and then performs one compatible send from the committed outbox (T3).
    * **T4 — succeeded:** since P06a ``succeeded`` means business handled and
      reply intents durably written; Feishu delivery state lives on the outbox.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        processor: EventProcessor,
        *,
        worker_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor
        self._worker_enabled = worker_enabled

    async def claim(
        self,
        event_id: str,
        event: dict[str, Any],
        *,
        transport: str,
    ) -> bool:
        """T1 claim only; the worker owns processing. False when already claimed."""
        received_at = datetime.now(UTC)
        message_id = message_id_from_event(event)
        try:
            payload = serialize_payload(
                build_stored_payload(
                    event_id,
                    event,
                    transport=transport,
                    received_at=received_at,
                )
            )
        except EventPayloadError:
            logger.exception(
                "refusing to claim event with invalid payload shape "
                "event_id=%s transport=%s message_id=%s",
                event_id,
                transport,
                message_id,
            )
            raise

        # T1: claim with durable payload. attempt_count starts at 0; the worker
        # (or the sync PROCESSING transition) counts the first attempt.
        async with self._session_factory() as session:
            session.add(
                ProcessedEvent(
                    event_id=event_id,
                    payload_json=payload,
                    payload_version=int(payload["payload_version"]),
                    transport=transport,
                    status=EventProcessStatus.RECEIVED.value,
                    attempt_count=0,
                    manual_replay_count=0,
                    replay_safety_version=REPLAY_SAFETY_VERSION,
                    source_message_id=message_id,
                    user_open_id=user_open_id_from_event(event),
                    received_at=received_at,
                    last_error_code=None,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True

    async def handle(
        self,
        event_id: str,
        event: dict[str, Any],
        *,
        transport: str,
    ) -> bool:
        """Claim and synchronously process an event (legacy sync path)."""
        if not await self.claim(event_id, event, transport=transport):
            return False

        await self._mark_status(event_id, EventProcessStatus.PROCESSING)

        # T2: process from DB-round-tripped payload (not the in-memory original).
        try:
            business_event = await self._load_business_event(event_id)
            await self._processor.process(business_event)
        except Exception as exc:
            await self._mark_status(
                event_id,
                EventProcessStatus.FAILED,
                last_error_code=_error_code(exc),
                result_summary=safe_error_summary(exc),
            )
            raise

        await self._mark_status(event_id, EventProcessStatus.SUCCEEDED)
        return True

    async def handle_safely(
        self,
        event_id: str,
        event: dict[str, Any],
        *,
        transport: str,
    ) -> None:
        message_id = message_id_from_event(event)
        if self._worker_enabled:
            # Worker mode: claim only. Processing happens in the background
            # EventWorker, so this returns as soon as the durable payload is
            # stored and never blocks on AI, Feishu, or ledger work.
            try:
                await self.claim(event_id, event, transport=transport)
            except Exception:
                logger.exception(
                    "failed to claim Feishu event for worker "
                    "event_id=%s transport=%s message_id=%s",
                    event_id,
                    transport,
                    message_id,
                )
            return
        try:
            await self.handle(event_id, event, transport=transport)
        except Exception:
            logger.exception(
                "failed to process Feishu event event_id=%s transport=%s message_id=%s",
                event_id,
                transport,
                message_id,
            )

    async def _load_business_event(self, event_id: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = await session.get(ProcessedEvent, event_id)
            if row is None:
                raise EventPayloadError(f"claimed event missing from database: {event_id}")
            if row.payload_json is None:
                raise EventPayloadError(
                    f"event {event_id} has no payload and is not replayable"
                )
            parsed = parse_stored_payload(row.payload_json)
            return business_event_from_payload(parsed)

    async def _mark_status(
        self,
        event_id: str,
        status: EventProcessStatus,
        *,
        last_error_code: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        # Application-layer guard: only closed enum members may be written to the
        # status column, so arbitrary strings cannot leak into event state.
        if not isinstance(status, EventProcessStatus):
            raise TypeError(
                f"status must be an EventProcessStatus member, got {status!r}"
            )
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProcessedEvent).where(ProcessedEvent.event_id == event_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return
            row.status = status.value
            if status is EventProcessStatus.PROCESSING:
                row.attempt_count = (row.attempt_count or 0) + 1
            if last_error_code is not None:
                row.last_error_code = last_error_code[:64]
            if result_summary is not None:
                row.result_summary = result_summary[:MAX_RESULT_SUMMARY_LENGTH]
            if status is EventProcessStatus.SUCCEEDED:
                row.last_error_code = None
                row.result_summary = None
            await session.commit()


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    return name[:64]
