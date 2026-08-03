import json
from datetime import UTC, datetime

import httpx

from lark_ledger.config import Settings
from lark_ledger.models import Direction
from lark_ledger.schemas import Action
from lark_ledger.services.ai import AIInterpreter


async def test_interpreter_uses_strict_schema() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "create",
                                    "amount": "9.00",
                                    "direction": "expense",
                                    "category": "餐饮",
                                    "note": "糖水",
                                    "occurred_at": "2026-08-02T12:00:00+08:00",
                                    "range_start": None,
                                    "range_end": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ai.example/v1"
    )
    interpreter = AIInterpreter(Settings(ai_api_key="test-key"), client)
    command = await interpreter.interpret("糖水9块", now=datetime(2026, 8, 2, tzinfo=UTC))
    await client.aclose()

    assert command.action is Action.CREATE
    assert command.direction is Direction.EXPENSE
    response_format = captured["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["json_schema"]["strict"] is True  # type: ignore[index]
    assert "report" in json.dumps(response_format)
    assert "set_budget" in json.dumps(response_format)
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "查看月预算" in messages[0]["content"]


async def test_transcription() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert b"voice.opus" in request.content
        return httpx.Response(200, json={"text": "午饭 32"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ai.example/v1"
    )
    interpreter = AIInterpreter(Settings(ai_api_key="test-key"), client)
    assert await interpreter.transcribe(b"audio", "voice.opus") == "午饭 32"
    await client.aclose()
