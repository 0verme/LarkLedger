from typing import Any
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from lark_ledger.api import get_event_service, router
from lark_ledger.config import Settings, get_settings


def build_app(settings: Settings, service: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.event_service = service
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_event_service] = lambda: service
    return app


async def post(app: FastAPI, payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/feishu", json=payload)


async def test_webhook_keeps_url_verification() -> None:
    service = AsyncMock()
    response = await post(
        build_app(
            Settings(_env_file=None, event_mode="webhook", lark_verification_token="token"),
            service,
        ),
        {"type": "url_verification", "token": "token", "challenge": "challenge-value"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


async def test_webhook_dispatches_message_to_shared_event_service() -> None:
    service = AsyncMock()
    event = {"message": {"message_id": "om_1"}}
    response = await post(
        build_app(Settings(_env_file=None, event_mode="webhook"), service),
        {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt_hook"},
            "event": event,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"code": 0}
    service.handle_safely.assert_awaited_once_with("evt_hook", event)


async def test_websocket_mode_does_not_expose_webhook_or_require_verification_token() -> None:
    service = AsyncMock()
    settings = Settings(
        _env_file=None,
        event_mode="websocket",
        lark_app_id="cli_test",
        lark_app_secret="secret",
        lark_verification_token="",
    )
    response = await post(build_app(settings, service), {"anything": "ignored"})
    assert response.status_code == 404
    service.handle_safely.assert_not_awaited()
