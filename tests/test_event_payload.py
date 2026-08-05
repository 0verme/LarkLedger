import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    EventPayloadError,
    EventProcessStatus,
    build_stored_payload,
    business_event_from_payload,
    is_replayable_payload,
    normalize_business_event,
    parse_stored_payload,
    serialize_payload,
)
from lark_ledger.models import Base, ProcessedEvent
from lark_ledger.services.events import EventService


def sample_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "sender": {
            "sender_id": {
                "open_id": "ou_user",
                "user_id": "uid_user",
                "union_id": "on_should_not_persist",
            },
            "sender_type": "user",
            "tenant_key": "tenant_should_not_persist",
        },
        "message": {
            "message_id": "om_text",
            "message_type": "text",
            "chat_id": "oc_chat",
            "create_time": "1720000000000",
            "content": json.dumps({"text": "午饭32元"}, ensure_ascii=False),
            "mentions": [{"id": "ou_bot"}],
        },
    }
    event.update(overrides)
    return event


def test_normalize_strips_non_business_fields() -> None:
    normalized = normalize_business_event(sample_event())
    assert normalized == {
        "sender": {"sender_id": {"open_id": "ou_user", "user_id": "uid_user"}},
        "message": {
            "message_id": "om_text",
            "message_type": "text",
            "chat_id": "oc_chat",
            "content": json.dumps({"text": "午饭32元"}, ensure_ascii=False),
        },
    }
    assert "union_id" not in normalized["sender"]["sender_id"]
    assert "tenant_key" not in normalized["sender"]
    assert "mentions" not in normalized["message"]
    assert "create_time" not in normalized["message"]


def test_normalize_image_keeps_resource_key_not_binary() -> None:
    event = sample_event()
    event["message"]["message_type"] = "image"
    event["message"]["content"] = json.dumps({"image_key": "img_abc"})
    normalized = normalize_business_event(event)
    content = json.loads(normalized["message"]["content"])
    assert content == {"image_key": "img_abc"}
    assert b"PNG" not in json.dumps(normalized).encode()


def test_payload_round_trip_preserves_business_fields() -> None:
    received_at = datetime(2026, 8, 5, 7, 0, tzinfo=UTC)
    payload = build_stored_payload(
        "evt_1",
        sample_event(),
        transport="webhook",
        received_at=received_at,
    )
    stored = serialize_payload(payload)
    parsed = parse_stored_payload(stored)
    business = business_event_from_payload(parsed)

    assert parsed["payload_version"] == PAYLOAD_VERSION
    assert parsed["event_id"] == "evt_1"
    assert parsed["transport"] == "webhook"
    assert parsed["received_at"] == received_at.isoformat()
    assert business["message"]["message_id"] == "om_text"
    assert json.loads(business["message"]["content"])["text"] == "午饭32元"
    assert business["sender"]["sender_id"]["open_id"] == "ou_user"


def test_unknown_payload_version_is_rejected() -> None:
    payload = build_stored_payload(
        "evt_1",
        sample_event(),
        transport="websocket",
        received_at=datetime(2026, 8, 5, 7, 0, tzinfo=UTC),
    )
    payload["payload_version"] = 99
    with pytest.raises(EventPayloadError, match="unsupported payload_version"):
        parse_stored_payload(payload)


def test_legacy_null_payload_is_not_replayable() -> None:
    assert not is_replayable_payload(None)
    with pytest.raises(EventPayloadError, match="not replayable"):
        parse_stored_payload(None)


def test_build_payload_rejects_naive_received_at() -> None:
    with pytest.raises(EventPayloadError, match="timezone-aware"):
        build_stored_payload(
            "evt_1",
            sample_event(),
            transport="webhook",
            received_at=datetime(2026, 8, 5, 7, 0),
        )


def test_webhook_and_websocket_normalize_to_same_event_shape() -> None:
    event = sample_event()
    received_at = datetime(2026, 8, 5, 7, 0, tzinfo=UTC)
    webhook = build_stored_payload(
        "evt_shared", event, transport="webhook", received_at=received_at
    )
    websocket = build_stored_payload(
        "evt_shared", event, transport="websocket", received_at=received_at
    )
    assert webhook["event"] == websocket["event"]
    assert webhook["transport"] != websocket["transport"]


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_once = False

    async def process(self, event: dict[str, Any]) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated processor failure")
        self.events.append(event)


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_claim_persists_replayable_payload_and_is_idempotent() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    service = EventService(factory, processor)
    event = sample_event()

    assert await service.handle("evt_shared", event, transport="webhook")
    assert not await service.handle("evt_shared", event, transport="webhook")
    assert len(processor.events) == 1
    assert processor.events[0]["message"]["message_id"] == "om_text"
    # Processor must receive normalized shape (no tenant_key / mentions).
    assert "tenant_key" not in processor.events[0].get("sender", {})
    assert "mentions" not in processor.events[0]["message"]

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt_shared")
        assert row is not None
        assert row.payload_json is not None
        assert row.payload_version == PAYLOAD_VERSION
        assert row.transport == "webhook"
        assert row.status == EventProcessStatus.SUCCEEDED.value
        assert row.received_at is not None
        assert row.last_error_code is None
        parsed = parse_stored_payload(row.payload_json)
        assert business_event_from_payload(parsed)["message"]["message_id"] == "om_text"

    await engine.dispose()


async def test_websocket_transport_is_recorded() -> None:
    engine, factory = await _sqlite_factory()
    service = EventService(factory, RecordingProcessor())
    assert await service.handle("evt_ws", sample_event(), transport="websocket")
    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt_ws")
        assert row is not None
        assert row.transport == "websocket"
        assert row.payload_json is not None
        assert row.payload_json["transport"] == "websocket"
    await engine.dispose()


async def test_processor_failure_marks_failed_without_second_execution() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    processor.fail_once = True
    service = EventService(factory, processor)

    with pytest.raises(RuntimeError, match="simulated processor failure"):
        await service.handle("evt_fail", sample_event(), transport="webhook")

    # Already claimed: second delivery must not re-run processor.
    assert not await service.handle("evt_fail", sample_event(), transport="webhook")
    assert processor.events == []

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt_fail")
        assert row is not None
        assert row.status == EventProcessStatus.FAILED.value
        assert row.last_error_code == "RuntimeError"
        assert row.payload_json is not None

    await engine.dispose()


async def test_claim_db_failure_does_not_run_processor() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()

    class FailingSessionFactory:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def add(self, _obj: object) -> None:
            return None

        async def commit(self) -> None:
            raise RuntimeError("db down")

        async def rollback(self) -> None:
            return None

    service = EventService(FailingSessionFactory(), processor)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="db down"):
        await service.handle("evt_db", sample_event(), transport="webhook")
    assert processor.events == []
    await engine.dispose()


async def test_legacy_rows_without_payload_remain_valid() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id="evt_legacy",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
                payload_json=None,
                payload_version=None,
                transport=None,
            )
        )
        await session.commit()
        query = select(ProcessedEvent).where(ProcessedEvent.event_id == "evt_legacy")
        rows = (await session.execute(query)).scalars().all()
        assert len(rows) == 1
        assert rows[0].payload_json is None
        assert rows[0].status == EventProcessStatus.LEGACY_SUCCEEDED.value
    await engine.dispose()


async def test_handle_safely_logs_without_payload_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    processor.fail_once = True
    service = EventService(factory, processor)
    secret_text = "午饭32元-机密备注"
    event = sample_event()
    event["message"]["content"] = json.dumps({"text": secret_text}, ensure_ascii=False)

    with caplog.at_level("ERROR"):
        await service.handle_safely("evt_log", event, transport="webhook")

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "evt_log" in joined
    assert "om_text" in joined
    assert "webhook" in joined
    assert secret_text not in joined
    assert "午饭32" not in joined
    await engine.dispose()
