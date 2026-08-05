import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import (
    EventPayloadError,
    EventProcessStatus,
    build_stored_payload,
    business_event_from_payload,
    message_id_from_event,
    parse_stored_payload,
    serialize_payload,
)
from lark_ledger.models import ProcessedEvent

logger = logging.getLogger(__name__)


class EventProcessor(Protocol):
    async def process(self, event: dict[str, Any]) -> None: ...


class EventService:
    """Shared idempotent entry point for all Feishu event transports.

    Transaction boundary (v0.2.0 / P00):

    * **T1 — claim:** insert ``processed_events`` with a versioned, normalized
      payload and commit. Primary-key conflict means the event was already claimed.
    * **T2 — process:** synchronously run the processor on a payload reloaded from
      the database (round-trip contract for future workers).

    T2 failures do **not** unclaim the event and are **not** retried in this
    version. Status may be ``failed`` for observability only.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        processor: EventProcessor,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor

    async def handle(
        self,
        event_id: str,
        event: dict[str, Any],
        *,
        transport: str,
    ) -> bool:
        """Claim and process an event, returning False when it was already claimed."""
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

        # T1: claim with durable payload.
        async with self._session_factory() as session:
            session.add(
                ProcessedEvent(
                    event_id=event_id,
                    payload_json=payload,
                    payload_version=int(payload["payload_version"]),
                    transport=transport,
                    status=EventProcessStatus.RECEIVED.value,
                    received_at=received_at,
                    last_error_code=None,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
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
    ) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProcessedEvent).where(ProcessedEvent.event_id == event_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return
            row.status = status.value
            if last_error_code is not None:
                row.last_error_code = last_error_code[:64]
            elif status is EventProcessStatus.SUCCEEDED:
                row.last_error_code = None
            await session.commit()


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    return name[:64]
