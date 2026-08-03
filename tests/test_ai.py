import json
from datetime import UTC, datetime

import httpx
import pytest

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
    assert "日元 JPY" in messages[0]["content"]


async def test_transcription() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer asr-key"
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "午饭 32"}}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    interpreter = AIInterpreter(
        Settings(
            _env_file=None,
            transcription_api_key="asr-key",
            transcription_model="qwen3-asr-flash",
        ),
        transcription_client=client,
    )
    assert await interpreter.transcribe(b"audio", "voice.opus") == "午饭 32"
    await client.aclose()

    assert captured["model"] == "qwen3-asr-flash"
    assert captured["stream"] is False
    assert captured["asr_options"] == {"enable_itn": True, "language": "zh"}
    audio_item = captured["messages"][0]["content"][0]  # type: ignore[index]
    assert audio_item["type"] == "input_audio"
    assert audio_item["input_audio"]["data"].startswith("data:audio/ogg;base64,")


@pytest.mark.parametrize(
    ("image", "media_type"),
    [
        (b"\xff\xd8\xffcontent", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\ncontent", "image/png"),
        (b"RIFF1234WEBPcontent", "image/webp"),
    ],
)
async def test_image_interpreter_uses_vision_api(image: bytes, media_type: str) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer vision-key"
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

    vision_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    interpreter = AIInterpreter(
        Settings(
            _env_file=None,
            ai_api_key="text-key",
            vision_api_key="vision-key",
            vision_model="qwen3.7-plus",
        ),
        vision_client=vision_client,
    )
    command = await interpreter.interpret(
        "识别图片并记账",
        now=datetime(2026, 8, 2, tzinfo=UTC),
        image=image,
    )
    await vision_client.aclose()

    assert command.action is Action.HELP
    assert captured["model"] == "qwen3.7-plus"
    assert captured["enable_thinking"] is False
    assert captured["response_format"] == {"type": "json_object"}
    image_item = captured["messages"][1]["content"][1]  # type: ignore[index]
    assert image_item["image_url"]["url"].startswith(f"data:{media_type};base64,")


@pytest.mark.parametrize("image", [b"", b"not-an-image"])
async def test_image_interpreter_rejects_invalid_media(image: bytes) -> None:
    interpreter = AIInterpreter(
        Settings(_env_file=None, vision_api_key="vision-key"),
    )
    with pytest.raises(ValueError):
        await interpreter.interpret(
            "识别图片并记账",
            now=datetime(2026, 8, 2, tzinfo=UTC),
            image=image,
        )


@pytest.mark.parametrize("filename", ["voice.opus", "voice.ogg"])
async def test_transcription_rejects_empty_audio(filename: str) -> None:
    interpreter = AIInterpreter(
        Settings(_env_file=None, transcription_api_key="asr-key"),
    )
    with pytest.raises(ValueError, match="不能为空"):
        await interpreter.transcribe(b"", filename)


async def test_transcription_rejects_invalid_response() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    interpreter = AIInterpreter(
        Settings(_env_file=None, transcription_api_key="asr-key"),
        transcription_client=client,
    )
    with pytest.raises(ValueError, match="返回格式无效"):
        await interpreter.transcribe(b"audio", "voice.ogg")
    await client.aclose()
