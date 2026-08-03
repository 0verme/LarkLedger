import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from lark_ledger.config import Settings
from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import Action, EntryCandidate, ParsedCommand
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.ledger import LedgerService


def batch_command(*items: EntryCandidate, truncated: bool = False) -> ParsedCommand:
    return ParsedCommand(
        action=Action.CREATE_ENTRIES,
        entries=list(items),
        batch_truncated=truncated,
    )


async def test_interpreter_parses_multiple_image_transactions() -> None:
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
                                    "action": "create_entries",
                                    "entries": [
                                        {
                                            "amount": "25.90",
                                            "currency": "CNY",
                                            "direction": "expense",
                                            "category": "餐饮",
                                            "note": "郑厨强麻辣烫",
                                            "occurred_at": "2026-08-03T12:10:00+08:00",
                                        },
                                        {
                                            "amount": "0.01",
                                            "currency": "CNY",
                                            "direction": "income",
                                            "category": "理财",
                                            "note": "余额宝收益",
                                            "occurred_at": "2026-08-03T02:28:00+08:00",
                                        },
                                    ],
                                    "batch_truncated": False,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://vision.example/v1"
    )
    interpreter = AIInterpreter(
        Settings(_env_file=None, vision_api_key="vision-key"),
        vision_client=client,
    )
    command = await interpreter.interpret(
        "识别这张支付截图并记账",
        now=datetime(2026, 8, 3, tzinfo=UTC),
        images=[b"\x89PNG\r\n\x1a\ncontent"],
    )
    await client.aclose()

    assert command.action is Action.CREATE_ENTRIES
    assert command.entries is not None
    assert len(command.entries) == 2
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "多个商品属于同一笔消费，不得拆成多笔" in messages[0]["content"]
    assert "最多返回前 30 笔" in messages[0]["content"]


def test_batch_schema_limits_items_and_preserves_single_create() -> None:
    single = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("9"),
        direction=Direction.EXPENSE,
        category="餐饮",
        occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    assert single.action is Action.CREATE

    with pytest.raises(ValidationError):
        batch_command(*(EntryCandidate(amount="1") for _ in range(31)))
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.CREATE_ENTRIES)
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.HELP, batch_truncated=True)


async def test_batch_persists_valid_items_and_reports_failures(session: Any) -> None:
    service = LedgerService(
        session,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )
    long_note = "很长的交易备注" * 8
    result = await service.execute(
        "ou_user",
        batch_command(
            EntryCandidate(
                amount="25.90",
                direction="expense",
                category="餐饮",
                note="郑厨强麻辣烫",
                occurred_at="2026-08-03T12:10:00+08:00",
            ),
            EntryCandidate(
                amount=None,
                direction="expense",
                category="购物",
                occurred_at="2026-08-03T09:21:00+08:00",
            ),
            EntryCandidate(
                amount="0.01",
                direction="income",
                category="理财",
                note=long_note,
                occurred_at="2026-08-03T02:28:00+08:00",
            ),
            truncated=True,
        ),
        source_type="image",
        source_message_id="om_batch_image",
    )

    assert "成功 2 笔，失败 1 笔" in result.message
    assert "收入合计 ¥0.01 · 支出合计 ¥25.90" in result.message
    assert "第 2 笔：缺少金额" in result.message
    assert "本次仅处理前 30 笔" in result.message
    assert "…" in result.message
    rows = (
        (
            await session.execute(
                select(LedgerEntry).order_by(LedgerEntry.source_item_index)
            )
        )
        .scalars()
        .all()
    )
    assert [row.source_item_index for row in rows] == [0, 2]
    assert [row.source_message_id for row in rows] == ["om_batch_image", "om_batch_image"]
    assert [row.amount for row in rows] == [Decimal("25.90"), Decimal("0.01")]


async def test_database_failure_isolated_to_one_batch_item(session: Any) -> None:
    service = LedgerService(session, now=datetime(2026, 8, 3, tzinfo=UTC))
    existing = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("10"),
        direction=Direction.EXPENSE,
        category="餐饮",
        occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    await service.execute(
        "ou_user",
        existing,
        source_type="image",
        source_message_id="om_partial_duplicate",
    )

    result = await service.execute(
        "ou_user",
        batch_command(
            EntryCandidate(
                amount="10",
                direction="expense",
                category="餐饮",
                occurred_at="2026-08-03T08:00:00+08:00",
            ),
            EntryCandidate(
                amount="20",
                direction="expense",
                category="交通",
                occurred_at="2026-08-03T09:00:00+08:00",
            ),
        ),
        source_type="image",
        source_message_id="om_partial_duplicate",
    )

    assert "成功 1 笔，失败 1 笔" in result.message
    assert "第 1 笔：保存失败" in result.message
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert sorted(row.amount for row in rows) == [Decimal("10.00"), Decimal("20.00")]


class FailingVisionInterpreter:
    vision_configured = True
    transcription_configured = False

    async def interpret(self, text: str, **kwargs: Any) -> ParsedCommand:
        raise httpx.TimeoutException("vision timed out")


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def download_resource(self, message_id: str, file_key: str, kind: str) -> bytes:
        return b"\x89PNG\r\n\x1a\ncontent"

    async def reply_text(self, message_id: str, text: str) -> None:
        self.texts.append(text)


class FailingDownloadFeishu(RecordingFeishu):
    async def download_resource(self, message_id: str, file_key: str, kind: str) -> bytes:
        raise httpx.HTTPStatusError(
            "forbidden",
            request=httpx.Request("GET", "https://open.feishu.cn/resource"),
            response=httpx.Response(403),
        )


async def test_processor_returns_stage_error_and_logs_reference(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        FailingVisionInterpreter(),  # type: ignore[arg-type]
    )

    with pytest.raises(httpx.TimeoutException):
        await processor.process(
            {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_failed_image",
                    "message_type": "image",
                    "content": json.dumps({"image_key": "img_1"}),
                },
            }
        )

    assert len(feishu.texts) == 1
    assert "图片识别服务调用失败" in feishu.texts[0]
    match = re.search(r"错误编号：([0-9A-F]{8})", feishu.texts[0])
    assert match is not None
    assert f"error_id={match.group(1)}" in caplog.text
    assert "stage=vision_interpretation" in caplog.text


async def test_processor_identifies_image_download_failure(
    session: Any, caplog: pytest.LogCaptureFixture
) -> None:
    feishu = FailingDownloadFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        lambda: session,  # type: ignore[arg-type]
        feishu,  # type: ignore[arg-type]
        FailingVisionInterpreter(),  # type: ignore[arg-type]
    )

    with pytest.raises(httpx.HTTPStatusError):
        await processor.process(
            {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_download_failure",
                    "message_type": "image",
                    "content": json.dumps({"image_key": "img_1"}),
                },
            }
        )

    assert "图片下载失败" in feishu.texts[0]
    assert "stage=image_download" in caplog.text


def test_stage_error_messages_are_actionable() -> None:
    expected = {
        "message_decode": "消息内容格式无效",
        "audio_download": "音频下载失败",
        "transcription": "语音转写失败",
        "interpretation": "指令识别服务调用失败",
        "persistence": "账目保存失败",
        "report_reply": "报告生成或发送失败",
    }
    for stage, text in expected.items():
        assert text in MessageProcessor._stage_error_message(stage)
