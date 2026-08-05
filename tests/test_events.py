from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.event_payload import EventProcessStatus, parse_stored_payload
from lark_ledger.models import Base, ProcessedEvent
from lark_ledger.services.events import EventService


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def process(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _event(message_id: str = "om_1") -> dict[str, Any]:
    return {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": '{"text":"hi"}',
        },
    }


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
    event = _event()

    assert await service.handle("evt_shared", event, transport="webhook")
    assert not await service.handle("evt_shared", event, transport="websocket")
    assert len(processor.events) == 1
    assert processor.events[0]["message"]["message_id"] == "om_1"

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt_shared")
        assert row is not None
        assert row.status == EventProcessStatus.SUCCEEDED.value
        assert row.transport == "webhook"
        assert row.payload_json is not None
        parsed = parse_stored_payload(row.payload_json)
        assert parsed["transport"] == "webhook"

    await engine.dispose()
