from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.models import Base
from lark_ledger.services.events import EventService


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def process(self, event: dict[str, Any]) -> None:
        self.events.append(event)


async def test_webhook_and_websocket_share_event_id_idempotency() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    processor = RecordingProcessor()
    service = EventService(factory, processor)
    event = {"message": {"message_id": "om_1"}}

    assert await service.handle("evt_shared", event)
    assert not await service.handle("evt_shared", event)
    assert processor.events == [event]
    await engine.dispose()
