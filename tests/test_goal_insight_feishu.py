"""P33 Feishu goal & insight commands end-to-end through the processor.

Deterministic path only: ``我的目标 / 目标 / 查看目标`` and
``洞察 / 财务洞察 / 本月洞察`` are parsed without AI and answered from
``ClientApplicationService`` — the numbers are computed, never guessed, and
the AI interpreter is never consulted (``NeverInterpreter`` fails loudly).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import AccountType, Base, Direction, LedgerEntry
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.goals import GoalService
from lark_ledger.services.identity import IdentityService


class NeverInterpreter:
    transcription_configured = False
    vision_configured = True

    async def interpret(self, text: str, *, now: datetime, images: list[bytes]) -> Any:
        raise AssertionError(f"goal/insight command reached AI interpreter: {text}")


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
        raise AssertionError("goal/insight command should not download media")


def _text_event(text: str, message_id: str = "om_goal") -> dict[str, Any]:
    return {
        "event_id": f"evt_{message_id}",
        "sender": {"sender_id": {"open_id": "ou_goal"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    }


async def _processor(
    factory: async_sessionmaker,
) -> tuple[MessageProcessor, RecordingFeishu]:
    settings = Settings(_env_file=None, reply_worker_enabled=False)
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        settings,
        factory,
        feishu,  # type: ignore[arg-type]
        NeverInterpreter(),  # type: ignore[arg-type]
    )
    return processor, feishu


async def _bootstrap(session: AsyncSession) -> None:
    context = await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_goal", display_name="我"
    )
    account = await AccountService(session).create(
        context,
        name="储蓄罐",
        account_type=AccountType.CASH,
        currency="CNY",
        opening_balance=Decimal("30000"),
    )
    await GoalService(session, timezone="Asia/Shanghai", currency="CNY").create(
        context,
        name="应急储备",
        target_amount=Decimal("60000"),
        account_ids=[account.id],
        target_date=date(2027, 3, 31),
    )
    now = datetime(2026, 8, 8, 4, tzinfo=UTC)
    for month_offset, amount in ((3, "1000"), (2, "1000"), (1, "1000")):
        session.add(
            LedgerEntry(
                user_open_id="ou_goal",
                created_by_user_id=context.actor_user_id,
                paid_by_user_id=context.actor_user_id,
                ledger_id=context.ledger_id,
                account_id=account.id,
                short_id=f"F{month_offset}",
                amount=Decimal(amount),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=now - timedelta(days=30 * month_offset),
                source_type="text",
            )
        )
    session.add(
        LedgerEntry(
            user_open_id="ou_goal",
            created_by_user_id=context.actor_user_id,
            paid_by_user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            account_id=account.id,
            short_id="F4",
            amount=Decimal("1500"),
            currency="CNY",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="",
            occurred_at=now - timedelta(days=1),
            source_type="text",
        )
    )
    await session.commit()


async def test_goal_keywords_reply_with_progress() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _bootstrap(session)
    processor, feishu = await _processor(factory)
    for keyword in ("我的目标", "目标", "查看目标"):
        await processor.process(_text_event(keyword, message_id=f"om_goal_{keyword}"))
        assert feishu.texts, f"no reply for {keyword}"
        reply = feishu.texts[-1]
        assert "🎯" in reply
        assert "应急储备" in reply
        assert "25500.00 / " in reply  # opening 30000 − 4500 seeded spending
        assert "42.5%" in reply
        assert "2027-03-31" in reply
    await engine.dispose()


async def test_goal_keyword_empty_state() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    processor, feishu = await _processor(factory)
    await processor.process(_text_event("我的目标", message_id="om_goal_empty"))
    assert feishu.texts
    assert "还没有财务目标" in feishu.texts[-1]
    await engine.dispose()


async def test_insight_keywords_reply_deterministically() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await _bootstrap(session)
    processor, feishu = await _processor(factory)
    for keyword in ("洞察", "财务洞察", "本月洞察"):
        await processor.process(_text_event(keyword, message_id=f"om_i_{keyword}"))
        assert feishu.texts, f"no reply for {keyword}"
        reply = feishu.texts[-1]
        assert "📌" in reply
        assert "餐饮" in reply  # spending-change insight from the seeded data
        assert "50.0%" in reply
    await engine.dispose()


async def test_insight_keyword_empty_state() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    processor, feishu = await _processor(factory)
    await processor.process(_text_event("洞察", message_id="om_i_empty"))
    assert feishu.texts
    assert "没有需要特别关注" in feishu.texts[-1]
    await engine.dispose()
