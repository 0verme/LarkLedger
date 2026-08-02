import asyncio
import json
from collections.abc import Callable
from typing import Any

from lark_ledger.config import Settings
from lark_ledger.services.websocket import LongConnectionReceiver, adapt_message_event


def message_payload(event_id: str = "evt_1") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "message_type": "text",
                "content": '{"text":"lunch 20"}',
            },
        },
    }


def test_adapt_long_connection_message_event() -> None:
    event_id, event = adapt_message_event(message_payload())
    assert event_id == "evt_1"
    assert event["message"]["message_id"] == "om_1"


def test_adapter_uses_sdk_marshaller_for_typed_event() -> None:
    marker = object()
    event_id, event = adapt_message_event(marker, lambda value: json.dumps(message_payload()))
    assert event_id == "evt_1"
    assert event["sender"]["sender_id"]["open_id"] == "ou_1"


class RecordingEventService:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.called = asyncio.Event()

    async def handle_safely(self, event_id: str, event: dict[str, Any]) -> None:
        self.events.append((event_id, event))
        self.called.set()


class FakeSdkClient:
    def __init__(self) -> None:
        self._auto_reconnect = True
        self.connected = False
        self.disconnected = False

    async def _connect(self) -> None:
        self.connected = True

    async def _ping_loop(self) -> None:
        await asyncio.Event().wait()

    async def _disconnect(self) -> None:
        self.disconnected = True


async def test_long_connection_start_callback_and_stop_without_network() -> None:
    service = RecordingEventService()
    client = FakeSdkClient()
    callback: Callable[[Any], None] | None = None

    def factory(value: Callable[[Any], None]) -> FakeSdkClient:
        nonlocal callback
        callback = value
        return client

    receiver = LongConnectionReceiver(
        Settings(
            _env_file=None,
            event_mode="websocket",
            lark_app_id="cli_test",
            lark_app_secret="secret",
        ),
        service,  # type: ignore[arg-type]
        client_factory=factory,
    )
    await receiver.start()
    assert receiver.status == "connected"
    assert callback is not None
    callback(message_payload("evt_ws"))
    await asyncio.wait_for(service.called.wait(), timeout=1)
    await receiver.stop()

    assert service.events[0][0] == "evt_ws"
    assert client.connected
    assert client.disconnected
    assert not client._auto_reconnect
    assert receiver.status == "stopped"
