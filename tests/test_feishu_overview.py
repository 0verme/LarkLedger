"""P31 Feishu household-overview command end-to-end tests through the processor.

Exercises the deterministic overview parser + ``MessageProcessor`` dispatch: a
bare "家庭概览" renders a compact text summary (period totals, budget progress,
member contributions, upcoming recurring) without ever reaching the AI
interpreter. The underlying aggregation is covered in ``test_household_overview``;
here we prove the command routes and the reply renders.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService


class NeverInterpreter:
    """Fails loudly if the overview command reaches the AI interpreter."""

    transcription_configured = False
    vision_configured = True

    async def interpret(self, text: str, *, now: datetime, images: list[bytes]) -> Any:
        raise AssertionError(f"overview command reached AI interpreter: {text}")


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
        raise AssertionError("overview command should not download media")


def _text_event(text: str, message_id: str = "om_overview") -> dict[str, Any]:
    return {
        "event_id": f"evt_{message_id}",
        "sender": {"sender_id": {"open_id": "ou_overview"}},
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


async def _seed_entry(factory: async_sessionmaker) -> None:
    """Book one confirmed expense as the bot user before asking for the overview."""
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_overview", display_name="小飞"
        )
        await LedgerService(session, commit_changes=False).execute(
            context,
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("32"),
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
                occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
            ),
        )
        await session.commit()


async def test_feishu_overview_renders_deterministic_summary() -> None:
    engine, factory = await _factory()
    processor, feishu = await _processor(factory)
    await _seed_entry(factory)

    await processor.process(_text_event("家庭概览"))
    assert feishu.texts, "expected an overview reply"
    reply = feishu.texts[-1]
    assert "概览" in reply
    assert "本月支出" in reply
    assert "¥32.00" in reply
    # The single owner shows up as a member contribution.
    assert "成员支出" in reply
    assert "小飞" in reply
    # No budget configured → no budget line.
    assert "预算" not in reply

    await engine.dispose()


async def test_feishu_overview_synonyms_and_fallback() -> None:
    engine, factory = await _factory()
    processor, feishu = await _processor(factory)
    await _seed_entry(factory)

    for text in ("概览", "家庭开销", "本月概览"):
        feishu.texts.clear()
        await processor.process(_text_event(text, f"om_{text}"))
        assert feishu.texts, f"expected a reply for {text}"
        assert "本月支出" in feishu.texts[-1]

    # An unrelated deterministic command (incomplete recurring rule) must fall
    # through to its own guidance reply, never the overview renderer.
    feishu.texts.clear()
    await processor.process(_text_event("每月8号房租", "om_fallback"))
    assert feishu.texts
    assert "周期账单" in feishu.texts[-1]
    assert "本月支出" not in feishu.texts[-1]

    await engine.dispose()
