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
from lark_ledger.models import Base, CategoryBudget
from lark_ledger.schemas import Action, BudgetCandidate, ParsedCommand
from lark_ledger.services.ai import AIInterpreter, CommandInterpretationError
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.ledger import LedgerService


def batch_command(*items: BudgetCandidate) -> ParsedCommand:
    return ParsedCommand(action=Action.SET_BUDGETS, budgets=list(items))


async def test_interpreter_parses_multiple_budgets_from_one_message() -> None:
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
                                    "action": "set_budgets",
                                    "budgets": [
                                        {"category": "交通", "amount": "500", "currency": None},
                                        {
                                            "category": "人情往来",
                                            "amount": "1000",
                                            "currency": None,
                                        },
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ai.example/v1"
    )
    interpreter = AIInterpreter(Settings(_env_file=None, ai_api_key="test-key"), client)
    command = await interpreter.interpret(
        "交通预算500 人情往来预算1000", now=datetime(2026, 8, 3, tzinfo=UTC)
    )
    await client.aclose()

    assert command.action is Action.SET_BUDGETS
    assert command.budgets is not None
    assert [item.category for item in command.budgets] == ["交通", "人情往来"]
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "必须解析成两个候选项" in messages[0]["content"]


async def test_interpreter_truncates_oversized_budget_batch() -> None:
    budgets = [
        {"category": f"分类{index}", "amount": "100", "currency": None}
        for index in range(11)
    ]
    response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "action": "set_budgets",
                            "budgets": budgets,
                            "budgets_truncated": False,
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        base_url="https://ai.example/v1",
    )
    command = await AIInterpreter(
        Settings(_env_file=None, ai_api_key="test-key"), client
    ).interpret("设置11项预算", now=datetime(2026, 8, 3, tzinfo=UTC))
    await client.aclose()

    assert command.budgets is not None
    assert len(command.budgets) == 10
    assert command.budgets_truncated is True


def test_batch_schema_limits_items_and_keeps_single_budget_compatible() -> None:
    single = ParsedCommand(action=Action.SET_BUDGET, category="交通", amount=Decimal("500"))
    assert single.action is Action.SET_BUDGET

    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.SET_BUDGETS,
            budgets=[BudgetCandidate(category=str(index), amount="1") for index in range(11)],
        )
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.SET_BUDGETS)


async def test_batch_sets_multiple_budgets_and_isolates_users(session: Any) -> None:
    service = LedgerService(session, now=datetime(2026, 8, 3, tzinfo=UTC))
    result = await service.execute(
        "ou_a",
        batch_command(
            BudgetCandidate(category="交通", amount="500"),
            BudgetCandidate(category="人情往来", amount="1000"),
        ),
    )

    assert "成功 2 项，失败 0 项" in result.message
    assert "交通" in result.message
    assert "人情往来" in result.message
    rows = (
        (
            await session.execute(
                select(CategoryBudget).order_by(CategoryBudget.category)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.user_open_id, row.category, row.amount) for row in rows] == [
        ("ou_a", "交通", Decimal("500.00")),
        ("ou_a", "人情往来", Decimal("1000.00")),
    ]
    other = await service.execute("ou_b", ParsedCommand(action=Action.LIST_BUDGETS))
    assert "没有设置任何月预算" in other.message


async def test_batch_budget_reports_truncation(session: Any) -> None:
    result = await LedgerService(session).execute(
        "ou_user",
        ParsedCommand(
            action=Action.SET_BUDGETS,
            budgets=[BudgetCandidate(category="交通", amount="500")],
            budgets_truncated=True,
        ),
    )

    assert "预算超过 10 项" in result.message
    assert "本次仅处理前 10 项" in result.message


async def test_batch_keeps_successes_when_other_items_fail(session: Any) -> None:
    rate_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        base_url="https://rates.example",
    )
    service = LedgerService(
        session,
        now=datetime(2026, 8, 3, tzinfo=UTC),
        exchange_rates=ExchangeRateService(Settings(_env_file=None), rate_client),
    )
    result = await service.execute(
        "ou_user",
        batch_command(
            BudgetCandidate(category="交通", amount="500"),
            BudgetCandidate(category="人情往来", amount="1000", currency="BTC"),
            BudgetCandidate(category="餐饮", amount="10000", currency="JPY"),
            BudgetCandidate(category="购物", amount=None),
        ),
    )
    await rate_client.aclose()

    assert "成功 1 项，失败 3 项" in result.message
    assert "人情往来：不支持币种 BTC" in result.message
    assert "餐饮：暂时无法获取汇率" in result.message
    assert "购物：缺少金额" in result.message
    rows = (await session.execute(select(CategoryBudget))).scalars().all()
    assert [(row.category, row.amount) for row in rows] == [("交通", Decimal("500.00"))]


async def test_duplicate_category_uses_last_item(session: Any) -> None:
    service = LedgerService(session)
    result = await service.execute(
        "ou_user",
        batch_command(
            BudgetCandidate(category="交通", amount="500"),
            BudgetCandidate(category="交通", amount="800"),
        ),
    )

    assert "成功 1 项，失败 0 项" in result.message
    budget = (await session.execute(select(CategoryBudget))).scalar_one()
    assert budget.amount == Decimal("800.00")


class FailingInterpreter:
    async def interpret(self, text: str, **kwargs: Any) -> ParsedCommand:
        raise CommandInterpretationError("invalid response")


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, message_id: str, text: str) -> None:
        self.texts.append(text)


async def test_processor_returns_actionable_command_validation_error() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FailingInterpreter(),  # type: ignore[arg-type]
    )

    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_batch",
                "message_type": "text",
                "content": json.dumps({"text": "交通预算500 人情往来预算1000"}),
            },
        }
    )
    await engine.dispose()

    assert len(feishu.texts) == 1
    assert "本次没有写入账本" in feishu.texts[0]
    assert "修改、撤销、查询或报告请单独发送" in feishu.texts[0]
    assert "处理失败了" not in feishu.texts[0]
