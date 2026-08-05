import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.feishu import FeishuClient, MessageProcessor
from lark_ledger.services.ledger import LedgerService


async def test_upload_image_and_reply_report_card() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path.endswith("/images"):
            assert b'content-disposition: form-data; name="image_type"' in request.content.lower()
            assert b"consumption-report.png" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_1"}})
        payload = json.loads(request.content)
        assert payload["msg_type"] == "interactive"
        assert json.loads(payload["content"])["schema"] == "2.0"
        return httpx.Response(200, json={"code": 0})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    )
    feishu = FeishuClient(
        Settings(lark_app_id="app", lark_app_secret="secret"),
        client,
    )
    image_key = await feishu.upload_image(b"\x89PNG\r\n\x1a\ncontent")
    await feishu.reply_card("om_1", {"schema": "2.0", "body": {"elements": []}})
    await client.aclose()

    assert image_key == "img_1"
    assert len(requests) == 3


@pytest.mark.parametrize("payload", [b"", b"not-png"])
async def test_upload_image_rejects_invalid_payload(payload: bytes) -> None:
    client = httpx.AsyncClient(base_url="https://open.feishu.cn")
    feishu = FeishuClient(Settings(), client)
    with pytest.raises(ValueError):
        await feishu.upload_image(payload)
    await client.aclose()


class ReportInterpreter:
    async def interpret(self, text: str, **kwargs: Any) -> ParsedCommand:
        return ParsedCommand(
            action=Action.REPORT,
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
            range_end=datetime(2026, 9, 1, tzinfo=UTC),
        )

    async def generate_advice(self, report: object) -> object:
        raise RuntimeError("AI unavailable")


class BudgetInterpreter:
    async def interpret(self, text: str, **kwargs: Any) -> ParsedCommand:
        return ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("80"),
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime.now(UTC),
        )


class ForeignCurrencyInterpreter:
    async def interpret(self, text: str, **kwargs: Any) -> ParsedCommand:
        return ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("1300"),
            currency="JPY",
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime.now(UTC),
        )


class StubRenderer:
    def render(self, report: object, advice: object) -> bytes:
        return b"\x89PNG\r\n\x1a\nreport"


class RecordingFeishu:
    def __init__(self, *, upload_fails: bool) -> None:
        self.upload_fails = upload_fails
        self.cards: list[dict[str, object]] = []
        self.texts: list[str] = []

    async def upload_image(self, png: bytes) -> str:
        if self.upload_fails:
            raise RuntimeError("upload unavailable")
        return "img_report"

    async def reply_card(self, message_id: str, card: dict[str, object]) -> None:
        self.cards.append(card)

    async def reply_text(self, message_id: str, text: str) -> None:
        self.texts.append(text)


@pytest.mark.parametrize(
    ("message_type", "content", "expected"),
    [
        ("image", {"image_key": "img_1"}, "图片识别功能尚未配置。"),
        ("audio", {"file_key": "file_1"}, "语音识别功能尚未配置。"),
    ],
)
async def test_processor_replies_when_media_api_is_not_configured(
    message_type: str,
    content: dict[str, str],
    expected: str,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    feishu = RecordingFeishu(upload_fails=False)
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        AIInterpreter(Settings(_env_file=None)),
    )

    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_media",
                "message_type": message_type,
                "content": json.dumps(content),
            },
        }
    )
    await engine.dispose()

    assert feishu.texts == [expected]
    assert feishu.cards == []


async def test_processor_replies_with_specific_exchange_rate_error() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    rate_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        base_url="https://rates.example",
    )
    feishu = RecordingFeishu(upload_fails=False)
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        ForeignCurrencyInterpreter(),  # type: ignore[arg-type]
        exchange_rates=ExchangeRateService(Settings(_env_file=None), rate_client),
    )
    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_fx",
                "message_type": "text",
                "content": json.dumps({"text": "午饭1300日元"}),
            },
        }
    )
    assert len(feishu.texts) == 1
    assert feishu.texts[0].startswith("暂时无法获取汇率，请稍后重试。")
    assert "错误编号：" in feishu.texts[0]
    await rate_client.aclose()
    await engine.dispose()


@pytest.mark.parametrize("upload_fails", [False, True])
async def test_processor_sends_one_report_card_with_fallback(upload_fails: bool) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await LedgerService(session).execute(
            "ou_user",
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("32"),
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
        )

    feishu = RecordingFeishu(upload_fails=upload_fails)
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        ReportInterpreter(),  # type: ignore[arg-type]
        StubRenderer(),  # type: ignore[arg-type]
    )
    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_report",
                "message_type": "text",
                "content": json.dumps({"text": "生成这个月的消费图表"}),
            },
        }
    )
    await engine.dispose()

    assert len(feishu.cards) == 1
    assert feishu.texts == []
    elements = feishu.cards[0]["body"]["elements"]  # type: ignore[index]
    has_image = any(element["tag"] == "img" for element in elements)  # type: ignore[union-attr]
    assert has_image is (not upload_fails)


async def test_processor_combines_entry_confirmation_and_budget_alert() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await LedgerService(session).execute(
            "ou_user",
            ParsedCommand(
                action=Action.SET_BUDGET,
                amount=Decimal("100"),
                category="餐饮",
            ),
        )

    feishu = RecordingFeishu(upload_fails=False)
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        BudgetInterpreter(),  # type: ignore[arg-type]
        StubRenderer(),  # type: ignore[arg-type]
    )
    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_budget",
                "message_type": "text",
                "content": json.dumps({"text": "午饭80"}),
            },
        }
    )
    await engine.dispose()

    assert len(feishu.texts) == 1
    assert "支出 ¥80.00 · 餐饮" in feishu.texts[0]
    assert "已记录 #" in feishu.texts[0]
    assert "餐饮本月预算快用完了" in feishu.texts[0]
