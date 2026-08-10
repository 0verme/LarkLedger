"""P29 Feishu recurring-rule command end-to-end tests through the processor.

Exercises the deterministic command parser + ``MessageProcessor`` dispatch: a
text like "每月8号房租3500" creates a rule with a reply, "我的周期账单" lists,
and 暂停 / 恢复 / 跳过 route to the rule lifecycle. Confirmation via the worker-
generated pending is covered in ``test_recurring_worker``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, RecurringRule
from lark_ledger.services.feishu import MessageProcessor


class NeverInterpreter:
    """Fails loudly if the recurring command reaches the AI interpreter."""

    transcription_configured = False
    vision_configured = True

    async def interpret(self, text: str, *, now: datetime, images: list[bytes]) -> Any:
        raise AssertionError(f"recurring command reached AI interpreter: {text}")


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, message_id: str, text: str, *, uuid: str | None = None) -> None:
        self.texts.append(text)

    async def reply_card(
        self, message_id: str, card: dict[str, Any], *, uuid: str | None = None
    ) -> None:
        self.texts.append(f"card:{card.get('header', {}).get('title', {}).get('content', '')}")

    async def download_resource(self, message_id: str, file_key: str, kind: str) -> bytes:
        raise AssertionError("recurring command should not download media")


def _text_event(text: str, message_id: str = "om_recur") -> dict[str, Any]:
    import json

    return {
        "event_id": f"evt_{message_id}",
        "sender": {"sender_id": {"open_id": "ou_recur"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    }


async def _factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _processor(factory: async_sessionmaker) -> tuple[MessageProcessor, RecordingFeishu]:
    settings = Settings(_env_file=None, reply_worker_enabled=False)
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        settings,
        factory,
        feishu,  # type: ignore[arg-type]
        NeverInterpreter(),  # type: ignore[arg-type]
        reply_worker_enabled=False,
    )
    return processor, feishu


async def test_feishu_create_list_pause_resume_skip() -> None:
    engine, factory = await _factory()
    processor, feishu = await _processor(factory)

    # Create a monthly rent rule.
    await processor.process(_text_event("每月8号房租3500", "om_create"))
    assert any("已创建周期账单" in text and "房租" in text for text in feishu.texts)
    async with factory() as session:
        rules = (await session.scalars(select(RecurringRule))).all()
        assert len(rules) == 1
        rule = rules[0]
        assert rule.category == "房租"
        assert rule.frequency == "monthly"
        assert rule.status == "active"

    # List.
    await processor.process(_text_event("我的周期账单", "om_list"))
    assert any("周期账单（1）" in text for text in feishu.texts)

    # Pause → resume → skip by name.
    await processor.process(_text_event("暂停房租", "om_pause"))
    async with factory() as session:
        assert (await session.scalar(select(RecurringRule))).status == "paused"
    await processor.process(_text_event("恢复房租", "om_resume"))
    async with factory() as session:
        assert (await session.scalar(select(RecurringRule))).status == "active"
    await processor.process(_text_event("跳过房租", "om_skip"))
    async with factory() as session:
        refreshed = await session.scalar(select(RecurringRule))
        assert refreshed.next_occurrence > rule.next_occurrence

    await engine.dispose()


async def test_feishu_create_income_with_currency() -> None:
    engine, factory = await _factory()
    processor, feishu = await _processor(factory)

    await processor.process(_text_event("每月1号工资到账10000", "om_income"))

    async with factory() as session:
        rule = await session.scalar(select(RecurringRule))
        assert rule.transaction_type.value == "income"
        assert rule.amount == 10000
    await engine.dispose()


async def test_feishu_near_miss_guides_instead_of_ai() -> None:
    engine, factory = await _factory()
    processor, feishu = await _processor(factory)

    # A recurring-shaped but incomplete message gets guidance, never AI.
    await processor.process(_text_event("每月8号房租", "om_bad"))
    assert any("周期账单" in text for text in feishu.texts)
    await engine.dispose()
