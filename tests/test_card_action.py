"""P07: interactive-card confirmation actions (confirm / cancel buttons)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import (
    Base,
    Direction,
    LedgerEntry,
    PendingCommand,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.card_action import CardActionService
from lark_ledger.services.pending import PendingCommandStore

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class RecordingFeishu:
    async def reply_text(self, message_id, text, *, uuid=None):
        pass

    async def reply_card(self, message_id, card, *, uuid=None):
        pass

    async def reply_file(self, message_id, file_key, *, uuid=None):
        pass

    async def upload_file(self, content, filename):
        return "file_key"

    async def upload_image(self, png):
        return "image_key"

    async def download_resource(self, message_id, file_key, kind):
        return b"\x89PNG\r\n\x1a\nimage"


class FixedInterpreter:
    transcription_configured = False
    vision_configured = True

    def __init__(self, command: ParsedCommand) -> None:
        self.command = command

    async def interpret(self, text, *, now, images):
        return self.command


class RecordingDeliverer:
    def __init__(self) -> None:
        self.delivered: list[Any] = []

    async def __call__(self, rows: list[Any]) -> None:
        self.delivered.extend(rows)


def _create_command() -> ParsedCommand:
    return ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32.00"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=T0,
    )


def _image_event(message_id: str, *, event_id: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id,
            "message_type": "image",
            "content": json.dumps({"image_key": "img_1"}),
        },
    }
    if event_id is not None:
        event["event_id"] = event_id
    return event


def _card_action(
    *,
    operator: str = "ou_user",
    code: str = "A83F2",
    action: str = "confirm",
    k: str = "larkledger_pending",
    message_id: str = "om_card",
) -> dict[str, Any]:
    return {
        "operator": {"open_id": operator},
        "action": {"value": {"k": k, "action": action, "code": code}, "tag": "button"},
        "context": {"open_message_id": message_id, "card_id": "card_1"},
    }


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _pending_code(factory: async_sessionmaker[Any]) -> str:
    from lark_ledger.services.feishu import MessageProcessor

    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        RecordingFeishu(),  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    await processor.process(_image_event("om_img", event_id="evt_img"))
    async with factory() as session:
        row = (await session.scalars(select(PendingCommand))).one()
    return row.confirmation_code


async def _entry_count(factory: async_sessionmaker[Any]) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(LedgerEntry))).scalar())


async def _service(
    factory: async_sessionmaker[Any],
) -> tuple[CardActionService, RecordingDeliverer]:
    deliverer = RecordingDeliverer()
    service = CardActionService(
        Settings(_env_file=None),
        PendingCommandStore(factory, Settings(_env_file=None)),
        exchange_rates=None,
        deliverer=deliverer,
    )
    return service, deliverer


async def test_card_confirm_executes_and_delivers() -> None:
    engine, factory = await _sqlite_factory()
    code = await _pending_code(factory)
    service, deliverer = await _service(factory)

    response = await service.handle_action(
        "evt_card", _card_action(code=code[1:], action="confirm")
    )
    assert response["toast"]["type"] == "success"
    assert await _entry_count(factory) == 1
    assert len(deliverer.delivered) == 1  # confirmation text outbox delivered
    async with factory() as session:
        row = (await session.scalars(select(PendingCommand))).one()
    assert row.status == "executed"
    await engine.dispose()


async def test_card_double_click_is_idempotent() -> None:
    engine, factory = await _sqlite_factory()
    code = await _pending_code(factory)
    service, deliverer = await _service(factory)

    await service.handle_action("evt_c1", _card_action(code=code[1:], action="confirm"))
    await service.handle_action("evt_c2", _card_action(code=code[1:], action="confirm"))

    assert await _entry_count(factory) == 1  # business executed exactly once
    assert len(deliverer.delivered) == 2  # both clicks got a reply (idempotent text)
    await engine.dispose()


async def test_card_cancel_writes_no_entry() -> None:
    engine, factory = await _sqlite_factory()
    code = await _pending_code(factory)
    service, deliverer = await _service(factory)

    response = await service.handle_action(
        "evt_card", _card_action(code=code[1:], action="cancel")
    )
    assert response["toast"]["type"] == "success"
    assert await _entry_count(factory) == 0
    async with factory() as session:
        row = (await session.scalars(select(PendingCommand))).one()
    assert row.status == "cancelled"
    await engine.dispose()


async def test_card_action_requires_operator_ownership() -> None:
    engine, factory = await _sqlite_factory()
    code = await _pending_code(factory)
    service, deliverer = await _service(factory)

    response = await service.handle_action(
        "evt_card", _card_action(operator="ou_other", code=code[1:], action="confirm")
    )
    assert response["toast"]["type"] == "success"  # reply still delivered
    assert await _entry_count(factory) == 0  # not the owner -> no execution
    async with factory() as session:
        row = (await session.scalars(select(PendingCommand))).one()
    assert row.status == "pending"
    await engine.dispose()


async def test_card_action_rejects_invalid_marker_or_action() -> None:
    engine, factory = await _sqlite_factory()
    service, _ = await _service(factory)

    response = await service.handle_action(
        "evt_card", _card_action(k="other_marker", action="confirm")
    )
    assert response["toast"]["type"] == "error"

    response = await service.handle_action(
        "evt_card", _card_action(action="unknown")
    )
    assert response["toast"]["type"] == "error"
    await engine.dispose()


async def test_card_action_rejects_invalid_code() -> None:
    engine, factory = await _sqlite_factory()
    service, _ = await _service(factory)

    response = await service.handle_action(
        "evt_card", _card_action(code="BAD", action="confirm")
    )
    assert response["toast"]["type"] == "error"
    assert await _entry_count(factory) == 0
    await engine.dispose()


async def test_card_action_on_expired_pending_does_not_execute() -> None:
    engine, factory = await _sqlite_factory()
    code = await _pending_code(factory)
    service, _ = await _service(factory)

    # Expire it first via the cleanup sweep.
    from lark_ledger.services.cleanup import CleanupStore

    far_future = T0 + timedelta(days=400)
    await CleanupStore(factory).expire_pending_batch(
        cutoff=far_future, now=far_future, batch_size=10
    )
    response = await service.handle_action(
        "evt_card", _card_action(code=code[1:], action="confirm")
    )
    assert response["toast"]["type"] == "success"  # idempotent reply delivered
    assert await _entry_count(factory) == 0
    await engine.dispose()
