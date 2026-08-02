import json
from datetime import UTC, datetime

import httpx

from lark_ledger.config import Settings
from lark_ledger.services.ai import AIInterpreter


async def test_interpreter_uses_json_object_for_deepseek() -> None:
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
                                    "action": "help",
                                    "amount": None,
                                    "direction": None,
                                    "category": None,
                                    "note": "",
                                    "occurred_at": None,
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
        transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com"
    )
    settings = Settings(
        ai_api_key="test-key",
        ai_base_url="https://api.deepseek.com",
        ai_model="deepseek-v4-flash",
    )
    interpreter = AIInterpreter(settings, client)
    await interpreter.interpret("help", now=datetime(2026, 8, 2, tzinfo=UTC))
    await client.aclose()

    assert captured["response_format"] == {"type": "json_object"}
