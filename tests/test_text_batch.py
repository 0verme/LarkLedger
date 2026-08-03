import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, CategoryBudget, Direction, LedgerEntry
from lark_ledger.schemas import Action, BudgetCandidate, EntryCandidate, ParsedCommand
from lark_ledger.services.ai import AIInterpreter, CommandInterpretationError
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.ledger import LedgerService

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)
COMPLEX_TEXT = (
    "今天早上支付宝买咖啡18块，早餐包子豆浆12块5，中午公司楼下吃饭36块，"
    "打车去客户那儿花了42块8，停车费15块，下午给小葡萄买绘本89块9，"
    "淘宝买数据线39块，晚上盒马买菜一共168块，其中用了20块优惠券，实际支付148块。"
    "对了，中午那顿饭不是36，是38块，帮我改一下。今天工资到账一万八，收到朋友还款500块，"
    "昨天买衣服299块。晚上和同事聚餐总共426块，我先付的，四个人AA，他们三个每人转给我106块5。"
    "另外公司报销到账680块。交了本月话费129块，服务器续费46块，ChatGPT订阅花了20美元，"
    "按今天汇率记成人民币。最后再记一笔，微信转给老婆1000块作为家庭生活费。"
    "餐饮预算设1000块。"
)


def complex_payload() -> dict[str, object]:
    def entry(
        amount: str,
        direction: str,
        category: str,
        note: str,
        *,
        currency: str | None = None,
        occurred_at: str = "2026-08-03T12:00:00+08:00",
    ) -> dict[str, object]:
        return {
            "amount": amount,
            "currency": currency,
            "direction": direction,
            "category": category,
            "note": note,
            "occurred_at": occurred_at,
        }

    entries = [
        entry("18", "expense", "餐饮", "咖啡"),
        entry("12.5", "expense", "餐饮", "包子豆浆"),
        entry("38", "expense", "餐饮", "午饭"),
        entry("42.8", "expense", "交通", "打车"),
        entry("15", "expense", "交通", "停车费"),
        entry("89.9", "expense", "购物", "绘本"),
        entry("39", "expense", "购物", "数据线"),
        entry("148", "expense", "餐饮", "盒马买菜"),
        entry("18000", "income", "工资", "工资到账"),
        entry("500", "income", "其他", "朋友还款"),
        entry("299", "expense", "购物", "买衣服", occurred_at="2026-08-02T12:00:00+08:00"),
        entry("426", "expense", "餐饮", "同事聚餐垫付"),
        entry("106.5", "income", "其他", "AA收款1"),
        entry("106.5", "income", "其他", "AA收款2"),
        entry("106.5", "income", "其他", "AA收款3"),
        entry("680", "income", "其他", "公司报销"),
        entry("129", "expense", "生活缴费", "话费"),
        entry("46", "expense", "其他", "服务器续费"),
        entry("20", "expense", "其他", "ChatGPT订阅", currency="USD"),
        entry("1000", "expense", "家庭", "家庭生活费"),
    ]
    return {
        "action": "batch",
        "entries": entries,
        "budgets": [{"category": "餐饮", "amount": "1000", "currency": None}],
        "batch_truncated": False,
        "budgets_truncated": False,
    }


async def test_interpreter_accepts_complex_text_batch_and_instructs_corrections() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(complex_payload(), ensure_ascii=False)}}
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ai.example/v1"
    )
    interpreter = AIInterpreter(Settings(_env_file=None, ai_api_key="test-key"), client)
    command = await interpreter.interpret(COMPLEX_TEXT, now=NOW)
    await client.aclose()

    assert command.action is Action.BATCH
    assert command.entries is not None
    assert command.budgets is not None
    assert len(command.entries) == 20
    assert len(command.budgets) == 1
    assert [item.amount for item in command.entries if item.note == "午饭"] == [Decimal("38")]
    assert [item.amount for item in command.entries if item.note == "盒马买菜"] == [
        Decimal("148")
    ]
    assert len([item for item in command.entries if str(item.note).startswith("AA收款")]) == 3
    messages = captured["messages"]
    assert isinstance(messages, list)
    prompt = messages[0]["content"]
    assert "以用户最后的修正为准" in prompt
    assert "必须展开为三笔独立收入" in prompt
    assert "修改、撤销、查询或报告动作" in prompt


class FixedExchangeRates:
    async def convert(self, amount: Decimal, source: str, target: str) -> Decimal:
        assert (source, target) == ("USD", "CNY")
        return (amount * Decimal("7.20")).quantize(Decimal("0.01"))


async def test_complex_batch_persists_entries_budget_and_totals(session: Any) -> None:
    command = ParsedCommand.model_validate(complex_payload())
    service = LedgerService(
        session,
        now=NOW,
        exchange_rates=FixedExchangeRates(),  # type: ignore[arg-type]
    )
    result = await service.execute(
        "ou_user",
        command,
        source_type="text",
        source_message_id="om_complex",
    )

    assert "账目成功 20 笔、失败 0 笔；预算成功 1 项、失败 0 项" in result.message
    assert "收入合计 ¥19499.50 · 支出合计 ¥2447.20" in result.message
    assert "由 20.00 USD 约算" in result.message
    entries = (
        (
            await session.execute(
                select(LedgerEntry).order_by(LedgerEntry.source_item_index)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 20
    assert [item.source_item_index for item in entries] == list(range(20))
    assert all(item.source_message_id == "om_complex" for item in entries)
    assert sum(item.amount for item in entries if item.direction is Direction.INCOME) == Decimal(
        "19499.50"
    )
    assert sum(item.amount for item in entries if item.direction is Direction.EXPENSE) == Decimal(
        "2447.20"
    )
    budget = (await session.execute(select(CategoryBudget))).scalar_one()
    assert (budget.category, budget.amount) == ("餐饮", Decimal("1000.00"))


async def test_text_batch_isolates_invalid_and_duplicate_entries(session: Any) -> None:
    service = LedgerService(session, now=NOW)
    existing = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("10"),
        direction=Direction.EXPENSE,
        category="餐饮",
        occurred_at=NOW,
    )
    await service.execute(
        "ou_user",
        existing,
        source_type="text",
        source_message_id="om_retry",
        source_item_index=0,
    )
    result = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.BATCH,
            entries=[
                EntryCandidate(
                    amount="10",
                    direction="expense",
                    category="餐饮",
                    occurred_at=NOW,
                ),
                EntryCandidate(direction="expense", category="交通", occurred_at=NOW),
                EntryCandidate(
                    amount="20",
                    direction="expense",
                    category="交通",
                    occurred_at=NOW,
                ),
            ],
            budgets=[
                BudgetCandidate(category="交通", amount="500"),
                BudgetCandidate(category="购物", amount=None),
            ],
        ),
        source_type="text",
        source_message_id="om_retry",
    )

    assert "账目成功 1 笔、失败 2 笔；预算成功 1 项、失败 1 项" in result.message
    assert "第 1 笔：保存失败" in result.message
    assert "第 2 笔：缺少金额" in result.message
    assert "购物：缺少金额" in result.message
    rows = (await session.execute(select(LedgerEntry))).scalars().all()
    assert sorted(row.amount for row in rows) == [Decimal("10.00"), Decimal("20.00")]
    budget = (await session.execute(select(CategoryBudget))).scalar_one()
    assert (budget.category, budget.amount) == ("交通", Decimal("500.00"))


def test_batch_schema_requires_supported_shapes_and_limits() -> None:
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.BATCH)
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.BATCH,
            entries=[EntryCandidate(amount="1") for _ in range(21)],
        )
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.BATCH,
            budgets=[BudgetCandidate(category=str(index), amount="1") for index in range(11)],
        )
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.BATCH,
            entries=[EntryCandidate(amount="1")],
            amount=Decimal("1"),
        )


async def test_batch_reports_entry_and_budget_truncation(session: Any) -> None:
    result = await LedgerService(session, now=NOW).execute(
        "ou_user",
        ParsedCommand(
            action=Action.BATCH,
            entries=[
                EntryCandidate(
                    amount="12",
                    direction="expense",
                    category="餐饮",
                    occurred_at=NOW,
                )
            ],
            budgets=[BudgetCandidate(category="餐饮", amount="1000")],
            batch_truncated=True,
            budgets_truncated=True,
        ),
    )

    assert "账目超过 20 笔" in result.message
    assert "预算超过 10 项" in result.message


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": '{"action":"batch"'}}]},
    ],
)
async def test_interpreter_classifies_incomplete_responses(
    response: dict[str, object],
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        base_url="https://ai.example/v1",
    )
    interpreter = AIInterpreter(Settings(_env_file=None, ai_api_key="test-key"), client)

    with pytest.raises(CommandInterpretationError):
        await interpreter.interpret(COMPLEX_TEXT, now=NOW)
    await client.aclose()


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, message_id: str, text: str) -> None:
        self.texts.append(text)


async def test_incomplete_response_replies_without_database_writes() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="https://ai.example/v1",
    )
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None, ai_api_key="test-key"),
        factory,
        feishu,  # type: ignore[arg-type]
        AIInterpreter(Settings(_env_file=None, ai_api_key="test-key"), client),
    )

    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_incomplete",
                "message_type": "text",
                "content": json.dumps({"text": COMPLEX_TEXT}, ensure_ascii=False),
            },
        }
    )
    async with factory() as database_session:
        entries = (await database_session.execute(select(LedgerEntry))).scalars().all()
        budgets = (await database_session.execute(select(CategoryBudget))).scalars().all()
    await client.aclose()
    await engine.dispose()

    assert entries == []
    assert budgets == []
    assert len(feishu.texts) == 1
    assert "本次没有写入账本" in feishu.texts[0]
