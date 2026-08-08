import asyncio
import json
import types
from collections.abc import Callable
from typing import Any

import pytest

from lark_ledger.config import Settings
from lark_ledger.services.websocket import (
    LongConnectionReceiver,
    adapt_card_action_event,
    adapt_message_event,
)


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


def card_action_payload(event_id: str = "evt_card") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "action": {
                "tag": "button",
                "value": {"k": "larkledger_pending", "action": "confirm", "code": "A83F2"},
            },
            "context": {"open_message_id": "om_card", "card_id": "card_1"},
        },
    }


def test_adapt_long_connection_message_event() -> None:
    event_id, event = adapt_message_event(message_payload())
    assert event_id == "evt_1"
    assert event["message"]["message_id"] == "om_1"


def test_adapt_card_action_event() -> None:
    event_id, event = adapt_card_action_event(card_action_payload())
    assert event_id == "evt_card"
    assert event["action"]["value"]["code"] == "A83F2"
    assert event["operator"]["open_id"] == "ou_1"


def test_adapt_card_action_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        adapt_card_action_event(message_payload())


def test_adapter_uses_sdk_marshaller_for_typed_event() -> None:
    marker = object()
    event_id, event = adapt_message_event(marker, lambda value: json.dumps(message_payload()))
    assert event_id == "evt_1"
    assert event["sender"]["sender_id"]["open_id"] == "ou_1"


def test_adapter_message_event_uses_sdk_marshaller(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_lark = types.SimpleNamespace(
        JSON=types.SimpleNamespace(marshal=lambda value: json.dumps(message_payload()))
    )
    monkeypatch.setattr("lark_ledger.services.websocket.import_module", lambda name: fake_lark)
    event_id, event = adapt_message_event(object())
    assert event_id == "evt_1"
    assert event["sender"]["sender_id"]["open_id"] == "ou_1"


def test_adapter_message_event_decoded_non_object() -> None:
    with pytest.raises(ValueError):
        adapt_message_event(object(), lambda value: "[1, 2]")


def test_adapter_message_event_malformed_payloads() -> None:
    with pytest.raises(ValueError):
        adapt_message_event({"event": {}})
    with pytest.raises(ValueError):
        adapt_message_event({"header": {}, "event": {}})
    with pytest.raises(ValueError):
        adapt_message_event({"header": {"event_type": "im.message.receive_v1"}, "event": {}})


def test_adapter_card_action_uses_sdk_marshaller(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_lark = types.SimpleNamespace(
        JSON=types.SimpleNamespace(marshal=lambda value: json.dumps(card_action_payload()))
    )
    monkeypatch.setattr("lark_ledger.services.websocket.import_module", lambda name: fake_lark)
    event_id, event = adapt_card_action_event(object())
    assert event_id == "evt_card"
    assert event["action"]["value"]["code"] == "A83F2"


def test_adapter_card_action_decoded_non_object() -> None:
    with pytest.raises(ValueError):
        adapt_card_action_event(object(), lambda value: "[1, 2]")


def test_adapter_card_action_malformed_payloads() -> None:
    with pytest.raises(ValueError):
        adapt_card_action_event({"event": {}})
    with pytest.raises(ValueError):
        adapt_card_action_event({"header": {}, "event": {}})
    with pytest.raises(ValueError):
        adapt_card_action_event({"header": {"event_type": "card.action.trigger"}, "event": {}})


class RecordingEventService:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str]] = []
        self.called = asyncio.Event()

    async def handle_safely(
        self,
        event_id: str,
        event: dict[str, Any],
        *,
        transport: str,
    ) -> None:
        self.events.append((event_id, event, transport))
        self.called.set()


class BlockingCardService:
    def __init__(self) -> None:
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.called = asyncio.Event()

    async def handle_action(self, event_id: str, action_event: dict[str, Any]) -> None:
        self.actions.append((event_id, action_event))
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


def make_receiver(
    service: Any | None = None,
    *,
    card_action_service: Any | None = None,
    client_factory: Callable[[Callable[[Any], None]], Any] | None = None,
) -> LongConnectionReceiver:
    return LongConnectionReceiver(
        Settings(
            _env_file=None,
            event_mode="websocket",
            lark_app_id="cli_test",
            lark_app_secret="secret",
        ),
        service if service is not None else RecordingEventService(),  # type: ignore[arg-type]
        card_action_service=card_action_service,
        client_factory=client_factory,
    )


async def test_long_connection_start_callback_and_stop_without_network() -> None:
    service = RecordingEventService()
    client = FakeSdkClient()
    callback: Callable[[Any], None] | None = None

    def factory(value: Callable[[Any], None]) -> FakeSdkClient:
        nonlocal callback
        callback = value
        return client

    receiver = make_receiver(service, client_factory=factory)
    await receiver.start()
    assert receiver.status == "connected"
    assert receiver.startup_error is None
    assert receiver.health_snapshot()["running"] is True
    assert callback is not None
    callback(message_payload("evt_ws"))
    await asyncio.wait_for(service.called.wait(), timeout=1)
    await receiver.stop()

    assert service.events[0][0] == "evt_ws"
    assert service.events[0][2] == "websocket"
    assert client.connected
    assert client.disconnected
    assert not client._auto_reconnect
    assert receiver.status == "stopped"
    health = receiver.health_snapshot()
    assert health["running"] is False
    assert health["stopping"] is True
    assert health["task_exception"] is False


async def test_start_with_card_service_consumes_card_actions() -> None:
    service = RecordingEventService()
    card_service = BlockingCardService()
    client = FakeSdkClient()

    def factory(value: Callable[[Any], None]) -> FakeSdkClient:
        return client

    receiver = make_receiver(service, card_action_service=card_service, client_factory=factory)
    await receiver.start()
    assert receiver.status == "connected"
    assert receiver._card_consumer_task is not None
    receiver._on_sdk_card_action(card_action_payload("evt_card_ws"))
    await asyncio.wait_for(card_service.called.wait(), timeout=1)
    assert card_service.actions[0][0] == "evt_card_ws"
    await receiver.stop()
    assert receiver.status == "stopped"


async def test_start_fails_when_client_factory_raises() -> None:
    def factory(value: Callable[[Any], None]) -> FakeSdkClient:
        raise RuntimeError("no sdk available")

    receiver = make_receiver(client_factory=factory)
    with pytest.raises(RuntimeError, match="failed to start"):
        await receiver.start()
    assert receiver.startup_error == "no sdk available"
    assert receiver.status == "stopped"


async def test_consume_task_result_records_exception() -> None:
    receiver = make_receiver()

    async def boom() -> None:
        raise RuntimeError("consumer exploded")

    task = asyncio.create_task(boom())
    await asyncio.gather(task, return_exceptions=True)
    receiver._consume_task_result(task)
    assert receiver._consumer_task_exception_code == "RuntimeError"
    assert receiver.status == "error"
    assert receiver._consumer_task_done is True


async def test_stop_warns_when_thread_does_not_exit() -> None:
    class StuckThread:
        def join(self, timeout: float) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    receiver = make_receiver()
    receiver._thread = StuckThread()  # type: ignore[assignment]
    await receiver.stop()
    assert receiver.status == "stopped"


async def test_stop_gathers_processing_tasks() -> None:
    receiver = make_receiver()

    async def noop() -> None:
        pass

    task = asyncio.create_task(noop())
    receiver._processing_tasks.add(task)
    await receiver.stop()
    assert receiver.status == "stopped"


def test_build_sdk_client_registers_handlers_and_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    calls: list[str] = []

    class FakeBuilder:
        def register_p2_im_message_receive_v1(self, callback: Any) -> None:
            calls.append("message")

        def register_p2_card_action_trigger(self, callback: Any) -> None:
            calls.append("card")

        def build(self) -> Any:
            return object()

    class FakeWsClient:
        def __init__(
            self, app_id: str, app_secret: str, *, event_handler: Any, log_level: Any
        ) -> None:
            self.app_id = app_id
            self.app_secret = app_secret
            self.event_handler = event_handler
            self.log_level = log_level

    fake_lark = types.SimpleNamespace(
        EventDispatcherHandler=types.SimpleNamespace(builder=lambda a, b: FakeBuilder()),
        ws=types.SimpleNamespace(Client=FakeWsClient),
        LogLevel=types.SimpleNamespace(WARNING="warning"),
    )
    fake_ws_client_module = types.SimpleNamespace()

    def fake_import(name: str) -> Any:
        if name == "lark_oapi.ws.client":
            return fake_ws_client_module
        return fake_lark

    monkeypatch.setattr("lark_ledger.services.websocket.import_module", fake_import)
    receiver = make_receiver(card_action_service=object())
    client = receiver._build_sdk_client(lambda data: None)
    assert client.app_id == "cli_test"
    assert client.app_secret == "secret"
    assert client.log_level == "warning"
    assert calls == ["message", "card"]
    assert fake_ws_client_module._ws_connect_kwargs() == {}


def test_on_reconnecting_and_reconnected() -> None:
    receiver = make_receiver()
    receiver._on_reconnecting()
    assert receiver.status == "reconnecting"
    receiver._on_reconnected()
    assert receiver.status == "connected"


def test_on_sdk_event_adaptation_failure_logs_and_returns() -> None:
    receiver = make_receiver()
    receiver._on_sdk_event({"header": {}})
    assert receiver._loop is None


def test_on_sdk_card_action_error_toast() -> None:
    receiver = make_receiver()
    result = receiver._on_sdk_card_action({"header": {}})
    assert result["toast"]["type"] == "error"


async def test_on_sdk_card_action_success_enqueues() -> None:
    receiver = make_receiver(card_action_service=object())
    receiver._loop = asyncio.get_running_loop()
    receiver._card_queue = asyncio.Queue()
    result = receiver._on_sdk_card_action(card_action_payload("evt_enq"))
    assert result["toast"]["type"] == "success"
    await asyncio.sleep(0)
    assert receiver._card_queue.qsize() == 1


async def test_client_lifecycle_retries_on_connect_error() -> None:
    class RetryClient(FakeSdkClient):
        def __init__(self, receiver: LongConnectionReceiver) -> None:
            super().__init__()
            self.receiver = receiver
            self.attempts = 0

        async def _connect(self) -> None:
            self.attempts += 1
            asyncio.get_running_loop().call_soon_threadsafe(self.receiver._stop_requested.set)
            raise ConnectionError("temporarily down")

    receiver = make_receiver()
    client = RetryClient(receiver)
    await receiver._client_lifecycle(client)
    assert client.attempts == 1
    assert receiver.status == "reconnecting"


async def test_client_lifecycle_cancels_receive_loops() -> None:
    class AutoStopClient(FakeSdkClient):
        def __init__(self, receiver: LongConnectionReceiver) -> None:
            super().__init__()
            self.receiver = receiver

        async def _connect(self) -> None:
            asyncio.get_running_loop().call_soon_threadsafe(self.receiver._stop_requested.set)

    receiver = make_receiver()
    client = AutoStopClient(receiver)

    async def _receive_message_loop() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_receive_message_loop(), name="feishu-receive")
    await receiver._client_lifecycle(client)
    assert task.cancelled()
    assert client.disconnected
    assert receiver.status == "connected"
