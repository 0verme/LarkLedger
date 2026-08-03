import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction, LedgerEntry
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ai import CommandInterpretationError
from lark_ledger.services.feishu import MAX_POST_IMAGES, MessageProcessor


class CapturingInterpreter:
    transcription_configured = False

    def __init__(self, *, vision_configured: bool = True, action: Action = Action.HELP) -> None:
        self.vision_configured = vision_configured
        self.action = action
        self.calls: list[tuple[str, list[bytes]]] = []

    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes]
    ) -> ParsedCommand:
        self.calls.append((text, images))
        if self.action is Action.CREATE:
            return ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("18.50"),
                direction=Direction.EXPENSE,
                category="交通",
                note="打车",
                occurred_at=now.astimezone(UTC),
            )
        return ParsedCommand(action=Action.HELP)


class InvalidVisionInterpreter(CapturingInterpreter):
    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes]
    ) -> ParsedCommand:
        raise CommandInterpretationError("invalid vision response")


class RecordingFeishu:
    def __init__(self, *, failing_key: str | None = None) -> None:
        self.failing_key = failing_key
        self.downloads: list[str] = []
        self.texts: list[str] = []

    async def download_resource(self, message_id: str, file_key: str, kind: str) -> bytes:
        self.downloads.append(file_key)
        if file_key == self.failing_key:
            raise httpx.HTTPStatusError(
                "forbidden",
                request=httpx.Request("GET", "https://open.feishu.cn/resource"),
                response=httpx.Response(403),
            )
        if file_key.endswith("2"):
            return b"\xff\xd8\xffcontent"
        return b"\x89PNG\r\n\x1a\ncontent"

    async def reply_text(self, message_id: str, text: str) -> None:
        self.texts.append(text)


def post_event(content: dict[str, Any], message_id: str = "om_post") -> dict[str, Any]:
    return {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id,
            "message_type": "post",
            "content": json.dumps(content, ensure_ascii=False),
        },
    }


def test_parse_post_content_collects_text_and_deduplicates_images() -> None:
    text, image_keys = MessageProcessor._parse_post_content(
        {
            "title": "八月账单",
            "content": [
                [
                    {"tag": "at", "user_id": "ou_bot", "user_name": "飞账"},
                    {"tag": "text", "text": "请按交通分类"},
                    {"tag": "a", "text": "，这是补充说明", "href": "https://example.com"},
                    {"tag": "img", "image_key": "img_1"},
                ],
                [
                    {"tag": "img", "image_key": "img_1"},
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "text", "text": "第二页"},
                            {"tag": "img", "image_key": "img_2"},
                        ],
                    },
                ],
                "invalid row",
                [{"tag": "emotion", "emoji_type": "SMILE"}, None],
            ],
        }
    )

    assert text == "八月账单\n请按交通分类，这是补充说明\n第二页"
    assert image_keys == ["img_1", "img_2"]


def test_parse_post_content_rejects_missing_body() -> None:
    with pytest.raises(ValueError, match="content 数组"):
        MessageProcessor._parse_post_content({"title": "账单"})


async def test_processor_sends_post_text_and_all_images_to_vision(session: Any) -> None:
    interpreter = CapturingInterpreter()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    await processor.process(
        post_event(
            {
                "title": "",
                "content": [
                    [
                        {"tag": "at", "user_id": "ou_bot", "user_name": "飞账"},
                        {"tag": "text", "text": "这两页都按交通分类"},
                    ],
                    [{"tag": "img", "image_key": "img_1"}],
                    [{"tag": "img", "image_key": "img_2"}],
                ],
            }
        )
    )

    assert feishu.downloads == ["img_1", "img_2"]
    assert len(interpreter.calls) == 1
    assert interpreter.calls[0][0] == "这两页都按交通分类"
    assert interpreter.calls[0][1] == [
        b"\x89PNG\r\n\x1a\ncontent",
        b"\xff\xd8\xffcontent",
    ]


async def test_text_only_post_uses_text_interpretation(session: Any) -> None:
    interpreter = CapturingInterpreter(vision_configured=False)
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    await processor.process(
        post_event({"title": "记账", "content": [[{"tag": "text", "text": "午饭18"}]]})
    )

    assert interpreter.calls == [("记账\n午饭18", [])]
    assert feishu.downloads == []


@pytest.mark.parametrize(
    "content",
    [
        {"title": "", "content": []},
        {"title": "", "content": [[{"tag": "at", "user_id": "ou_bot"}]]},
        {"title": "", "content": [[{"tag": "emotion", "emoji_type": "SMILE"}]]},
    ],
)
async def test_empty_or_unsupported_post_is_rejected(session: Any, content: dict[str, Any]) -> None:
    interpreter = CapturingInterpreter()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    await processor.process(post_event(content))

    assert interpreter.calls == []
    assert feishu.downloads == []
    assert feishu.texts == ["这条富文本中没有可识别的文字或图片。"]


async def test_post_over_image_limit_is_rejected_before_download(session: Any) -> None:
    interpreter = CapturingInterpreter()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )
    images = [[{"tag": "img", "image_key": f"img_{index}"}] for index in range(6)]

    await processor.process(post_event({"title": "", "content": images}))

    assert interpreter.calls == []
    assert feishu.downloads == []
    assert feishu.texts == [
        f"一条富文本消息最多处理 {MAX_POST_IMAGES} 张图片，请拆分后重新发送。"
    ]


async def test_post_with_images_requires_vision_configuration(session: Any) -> None:
    interpreter = CapturingInterpreter(vision_configured=False)
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    await processor.process(
        post_event({"title": "", "content": [[{"tag": "img", "image_key": "img_1"}]]})
    )

    assert interpreter.calls == []
    assert feishu.downloads == []
    assert feishu.texts == ["图片识别功能尚未配置。"]


async def test_post_download_failure_prevents_interpretation(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    interpreter = CapturingInterpreter()
    feishu = RecordingFeishu(failing_key="img_2")
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    with pytest.raises(httpx.HTTPStatusError):
        await processor.process(
            post_event(
                {
                    "title": "",
                    "content": [
                        [{"tag": "img", "image_key": "img_1"}],
                        [{"tag": "img", "image_key": "img_2"}],
                    ],
                }
            )
        )

    assert interpreter.calls == []
    assert "图片下载失败" in feishu.texts[0]
    assert "stage=image_download" in caplog.text


async def test_post_vision_parse_failure_uses_image_guidance(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    interpreter = InvalidVisionInterpreter()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    await processor.process(
        post_event(
            {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "识别这笔"}],
                    [{"tag": "img", "image_key": "img_1"}],
                ],
            }
        )
    )

    assert "没有完整识别图片中的交易" in feishu.texts[0]
    assert "stage=vision_interpretation" in caplog.text


async def test_post_entries_keep_post_source_type() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    interpreter = CapturingInterpreter(action=Action.CREATE)
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        interpreter,  # type: ignore[arg-type]
    )

    await processor.process(
        post_event(
            {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "这笔是打车"}],
                    [{"tag": "img", "image_key": "img_1"}],
                ],
            },
            message_id="om_post_source",
        )
    )

    async with factory() as session:
        row = (await session.execute(select(LedgerEntry))).scalar_one()
    await engine.dispose()
    assert row.source_type == "post"
    assert row.source_message_id == "om_post_source"
    assert row.source_item_index == 0
