from typing import Any

import pytest
from fastapi import FastAPI

from lark_ledger import main
from lark_ledger.config import Settings


async def test_websocket_mode_requires_app_credentials_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        event_mode="websocket",
        lark_app_id="",
        lark_app_secret="",
        lark_verification_token="",
        worker_enabled=False,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="LARK_LEDGER_LARK_APP_ID"):
        async with main.lifespan(FastAPI()):
            pass


async def test_websocket_lifespan_starts_and_stops_without_webhook_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        event_mode="websocket",
        lark_app_id="cli_test",
        lark_app_secret="secret",
        lark_verification_token="",
        worker_enabled=False,
    )
    states: list[str] = []

    class FakeReceiver:
        status = "connected"

        def __init__(self, receiver_settings: Settings, event_service: Any) -> None:
            assert receiver_settings is settings

        async def start(self) -> None:
            states.append("started")

        async def stop(self) -> None:
            states.append("stopped")

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "LongConnectionReceiver", FakeReceiver)

    app = FastAPI()
    async with main.lifespan(app):
        assert states == ["started"]
        assert app.state.long_connection.status == "connected"
    assert states == ["started", "stopped"]


async def test_webhook_lifespan_does_not_start_long_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, event_mode="webhook", worker_enabled=False)

    def unexpected_receiver(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("webhook mode must not create a long connection")

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "LongConnectionReceiver", unexpected_receiver)

    async with main.lifespan(FastAPI()):
        pass


async def test_lifespan_starts_and_stops_worker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, event_mode="webhook", worker_enabled=True)
    lifecycle: list[str] = []

    class FakeWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            lifecycle.append("constructed")

        def start(self) -> None:
            lifecycle.append("started")

        async def stop(self) -> None:
            lifecycle.append("stopped")

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "EventWorker", FakeWorker)

    app = FastAPI()
    async with main.lifespan(app):
        assert lifecycle == ["constructed", "started"]
        assert isinstance(app.state.event_worker, FakeWorker)
    assert lifecycle == ["constructed", "started", "stopped"]


async def test_lifespan_does_not_start_worker_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, event_mode="webhook", worker_enabled=False)

    def unexpected_worker(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("worker must not start when disabled")

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "EventWorker", unexpected_worker)

    app = FastAPI()
    async with main.lifespan(app):
        assert not hasattr(app.state, "event_worker")
