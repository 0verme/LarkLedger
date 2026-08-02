import logging
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.models import ProcessedEvent

logger = logging.getLogger(__name__)


class EventProcessor(Protocol):
    async def process(self, event: dict[str, Any]) -> None: ...


class EventService:
    """Shared idempotent entry point for all Feishu event transports."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        processor: EventProcessor,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor

    async def handle(self, event_id: str, event: dict[str, Any]) -> bool:
        """Claim and process an event, returning False when it was already claimed."""
        async with self._session_factory() as session:
            session.add(ProcessedEvent(event_id=event_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        await self._processor.process(event)
        return True

    async def handle_safely(self, event_id: str, event: dict[str, Any]) -> None:
        try:
            await self.handle(event_id, event)
        except Exception:
            logger.exception("failed to process Feishu event %s", event_id)
