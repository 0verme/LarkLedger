"""P07: high-risk writes create a pending confirmation + preview outbox."""

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
    ReplyOutbox,
)
from lark_ledger.schemas import Action, EntryCandidate, ParsedCommand
from lark_ledger.services.pending import (
    CARD_ACTION_KEY,
    PendingPreview,
    build_pending_preview_card,
)

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.files: list[str] = []
        self.downloaded: list[tuple[str, str, str]] = []

    async def reply_text(
        self, message_id: str, text: str, *, uuid: str | None = None
    ) -> None:
        self.texts.append(text)

    async def reply_card(
        self, message_id: str, card: dict[str, Any], *, uuid: str | None = None
    ) -> None:
        self.cards.append(card)

    async def reply_file(
        self, message_id: str, file_key: str, *, uuid: str | None = None
    ) -> None:
        self.files.append(file_key)

    async def upload_file(self, content: bytes, filename: str) -> str:
        return "file_key"

    async def upload_image(self, png: bytes) -> str:
        return "image_key"

    async def download_resource(
        self, message_id: str, file_key: str, kind: str
    ) -> bytes:
        self.downloaded.append((message_id, file_key, kind))
        return b"\x89PNG\r\n\x1a\nimage"


class FixedInterpreter:
    transcription_configured = False
    vision_configured = True

    def __init__(
        self, command: ParsedCommand | None = None
    ) -> None:
        self.command = command
        self.calls: list[tuple[str, list[bytes]]] = []

    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes]
    ) -> ParsedCommand:
        self.calls.append((text, images))
        if self.command is None:
            raise AssertionError("interpreter called with no command configured")
        return self.command


def _create_command(**kw: Any) -> ParsedCommand:
    base: dict[str, Any] = dict(
        action=Action.CREATE,
        amount=Decimal("32.00"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=T0,
    )
    base.update(kw)
    return ParsedCommand(**base)


def _batch_command() -> ParsedCommand:
    return ParsedCommand(
        action=Action.BATCH,
        entries=[
            EntryCandidate(
                amount="10",
                direction="expense",
                category="餐饮",
                occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
            ),
            EntryCandidate(
                amount="20",
                direction="income",
                category="工资",
                occurred_at=datetime(2026, 8, 3, 13, tzinfo=UTC),
            ),
        ],
    )


def _message_event(message_id: str, text: str, *, event_id: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    }
    if event_id is not None:
        event["event_id"] = event_id
    return event


def _image_event(
    message_id: str,
    *,
    event_id: str | None = None,
    image_key: str = "img_1",
    user_open_id: str = "ou_user",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "sender": {"sender_id": {"open_id": user_open_id}},
        "message": {
            "message_id": message_id,
            "message_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
    }
    if event_id is not None:
        event["event_id"] = event_id
    return event


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _outbox_rows(factory: async_sessionmaker[Any]) -> list[ReplyOutbox]:
    async with factory() as session:
        rows = (await session.execute(select(ReplyOutbox))).scalars().all()
        return list(rows)


async def _pending_rows(factory: async_sessionmaker[Any]) -> list[PendingCommand]:
    async with factory() as session:
        rows = (await session.execute(select(PendingCommand))).scalars().all()
        return list(rows)


async def _seed_entry(
    factory: async_sessionmaker[Any],
    *,
    amount: str = "32.00",
    category: str = "餐饮",
    note: str = "午饭",
    short_id: str = "A83F2",
) -> None:
    async with factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_user",
                short_id=short_id,
                amount=Decimal(amount),
                currency="CNY",
                direction=Direction.EXPENSE,
                category=category,
                note=note,
                occurred_at=T0,
                source_type="text",
            )
        )
        await session.commit()


async def _processor(
    factory: async_sessionmaker[Any],
    feishu: RecordingFeishu,
    command: ParsedCommand | None,
    *,
    pending_enabled: bool = True,
):
    from lark_ledger.services.feishu import MessageProcessor

    return MessageProcessor(
        Settings(_env_file=None, pending_enabled=pending_enabled),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(command),
    )


async def test_pending_keeps_frozen_ledger_after_current_ledger_switch() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command())

    await processor.process(_image_event("om_pending_original"))
    pending = (await _pending_rows(factory))[0]
    frozen_ledger_id = pending.ledger_id
    assert frozen_ledger_id is not None

    await processor.process(_message_event("om_create_travel", "创建账本 旅行"))
    await processor.process(_message_event("om_select_travel", "切换账本 旅行"))
    await processor.process(
        _message_event("om_confirm_original", f"确认 #C-{pending.confirmation_code[1:]}")
    )

    async with factory() as session:
        entry = (await session.scalars(select(LedgerEntry))).one()
        assert entry.ledger_id == frozen_ledger_id
        refreshed = await session.get(PendingCommand, pending.id)
        assert refreshed is not None
        assert refreshed.ledger_id == frozen_ledger_id
    await engine.dispose()


def test_pending_preview_card_uses_native_json_v2_callbacks() -> None:
    card = build_pending_preview_card(
        PendingPreview(code="CA83F2", display_code="#C-A83F2"),
        timezone="Asia/Shanghai",
    )

    assert card["schema"] == "2.0"
    elements = card["body"]["elements"]
    assert all(element["tag"] != "action" for element in elements)

    buttons = [element for element in elements if element["tag"] == "button"]
    assert [button["element_id"] for button in buttons] == [
        "confirm_pending",
        "cancel_pending",
    ]
    assert len({button["element_id"] for button in buttons}) == 2
    assert [button["behaviors"] for button in buttons] == [
        [
            {
                "type": "callback",
                "value": {
                    "k": CARD_ACTION_KEY,
                    "action": "confirm",
                    "code": "A83F2",
                },
            }
        ],
        [
            {
                "type": "callback",
                "value": {
                    "k": CARD_ACTION_KEY,
                    "action": "cancel",
                    "code": "A83F2",
                },
            }
        ],
    ]


def test_pending_preview_card_formats_utc_expiry_in_configured_timezone() -> None:
    card = build_pending_preview_card(
        PendingPreview(
            code="CA83F2",
            display_code="#C-A83F2",
            expires_at="2026-08-08T07:36:00+00:00",
        ),
        timezone="Asia/Shanghai",
    )

    header = card["body"]["elements"][0]["content"]
    assert "过期时间：2026-08-08 15:36" in header


# ---------------------------------------------------------------------------
# High-risk writes create a pending confirmation, not a ledger write
# ---------------------------------------------------------------------------


async def test_image_message_creates_pending_not_ledger_entry() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command())
    await processor.process(_image_event("om_img", event_id="evt_img"))

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    rows = await _outbox_rows(factory)
    assert entries == 0  # never written without confirmation
    assert len(pending) == 1
    assert pending[0].source_type == "image"
    assert pending[0].status == "pending"
    assert pending[0].command_type == "create"
    assert len(rows) == 1
    assert rows[0].reply_type == "card"
    assert "待确认" in json.dumps(rows[0].payload_json, ensure_ascii=False)
    assert feishu.downloaded == [("om_img", "img_1", "image")]
    await engine.dispose()


async def test_batch_message_creates_pending_not_ledger_entry() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _batch_command())
    await processor.process(_message_event("om_b", "早餐10 工资到账20", event_id="evt_b"))

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    rows = await _outbox_rows(factory)
    assert entries == 0
    assert len(pending) == 1
    assert pending[0].source_type == "text"
    assert pending[0].command_type == "batch"
    assert pending[0].source_message_id == "om_b"
    assert len(rows) == 1
    await engine.dispose()


async def test_duplicate_text_write_creates_pending() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, note="午饭")
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command(note="午饭"))
    await processor.process(_message_event("om_dup", "午饭32", event_id="evt_dup"))

    pending = await _pending_rows(factory)
    assert len(pending) == 1
    assert pending[0].risk_reason == "duplicate"
    preview = pending[0].preview_json
    assert any("疑似与账目 #A83F2 重复" in a for a in preview["anomalies"])
    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    assert entries == 1  # only the seeded entry
    await engine.dispose()


# ---------------------------------------------------------------------------
# Simple text stays write-through
# ---------------------------------------------------------------------------


async def test_simple_text_create_writes_through() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command())
    await processor.process(_message_event("om_1", "午饭32元", event_id="evt_1"))

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    rows = await _outbox_rows(factory)
    assert entries == 1
    assert pending == []
    assert len(rows) == 1
    assert rows[0].reply_type == "text"
    await engine.dispose()


# ---------------------------------------------------------------------------
# Crash-window: re-delivery does not create a second pending
# ---------------------------------------------------------------------------


async def test_redelivered_event_does_not_create_second_pending() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command())
    await processor.process(_image_event("om_img", event_id="evt_img"))
    await processor.process(_image_event("om_img", event_id="evt_img"))  # re-claim

    pending = await _pending_rows(factory)
    rows = await _outbox_rows(factory)
    assert len(pending) == 1  # business_committed_at + outbox pre-check skip re-create
    assert len(rows) == 1
    await engine.dispose()


async def test_same_image_with_different_event_and_message_is_suppressed() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    interpreter = FixedInterpreter(_create_command())

    from lark_ledger.services.feishu import MessageProcessor

    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        interpreter,
    )
    await processor.process(_image_event("om_img_1", event_id="evt_img_1"))
    await processor.process(_image_event("om_img_2", event_id="evt_img_2"))

    pending = await _pending_rows(factory)
    rows = await _outbox_rows(factory)
    assert len(pending) == 1
    assert pending[0].source_fingerprint is not None
    assert len(rows) == 1
    assert len(feishu.cards) == 1
    assert len(interpreter.calls) == 1
    await engine.dispose()


async def test_same_image_can_be_sent_again_after_cancellation() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command())
    await processor.process(_image_event("om_img_1", event_id="evt_img_1"))
    first = (await _pending_rows(factory))[0]

    from lark_ledger.services.pending import PendingCommandStore

    await PendingCommandStore(factory, Settings(_env_file=None)).cancel(
        user_open_id="ou_user",
        confirmation_code=first.confirmation_code,
        reply_to_message_id="om_cancel",
        cancel_event_id=None,
        now=T0,
    )
    await processor.process(_image_event("om_img_2", event_id="evt_img_2"))

    pending = await _pending_rows(factory)
    assert len(pending) == 2
    assert {row.status for row in pending} == {"cancelled", "pending"}
    await engine.dispose()


async def test_different_image_or_user_creates_separate_pending() -> None:
    class KeyedFeishu(RecordingFeishu):
        async def download_resource(
            self, message_id: str, file_key: str, kind: str
        ) -> bytes:
            self.downloaded.append((message_id, file_key, kind))
            return file_key.encode()

    engine, factory = await _sqlite_factory()
    feishu = KeyedFeishu()
    processor = await _processor(factory, feishu, _create_command())
    await processor.process(
        _image_event("om_img_1", event_id="evt_img_1", image_key="img_1")
    )
    await processor.process(
        _image_event("om_img_2", event_id="evt_img_2", image_key="img_2")
    )
    await processor.process(
        _image_event(
            "om_img_3",
            event_id="evt_img_3",
            image_key="img_1",
            user_open_id="ou_other",
        )
    )

    pending = await _pending_rows(factory)
    assert len(pending) == 3
    assert len({(row.user_open_id, row.source_fingerprint) for row in pending}) == 3
    await engine.dispose()


async def test_pending_row_frozen_payload_and_code() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    command = _create_command()
    processor = await _processor(factory, feishu, command)
    await processor.process(_image_event("om_img", event_id="evt_img"))

    pending = await _pending_rows(factory)
    code = pending[0].confirmation_code
    assert len(code) == 6
    assert code[0] == "C"
    assert pending[0].payload_json == command.model_dump(mode="json")
    assert pending[0].source_event_id == "evt_img"
    assert pending[0].expires_at is not None
    await engine.dispose()


async def test_pending_disabled_writes_through_image() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = await _processor(factory, feishu, _create_command(), pending_enabled=False)
    await processor.process(_image_event("om_img", event_id="evt_img"))

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    assert entries == 1
    assert pending == []
    await engine.dispose()


# ---------------------------------------------------------------------------
# Confirmation directives (确认/取消/查看待确认)
# ---------------------------------------------------------------------------


async def _create_pending_via_image(factory: async_sessionmaker[Any]) -> str:
    """Create a pending via an image message and return its confirmation code."""
    from lark_ledger.services.feishu import MessageProcessor

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    await processor.process(_image_event("om_img", event_id="evt_img"))
    pending = await _pending_rows(factory)
    assert len(pending) == 1
    return pending[0].confirmation_code


async def _confirm_text(code: str) -> str:
    return f"确认 #C-{code[1:]}"


async def test_confirm_executes_frozen_command_and_writes_entry() -> None:
    engine, factory = await _sqlite_factory()
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.feishu import MessageProcessor

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(),
    )
    await processor.process(
        _message_event("om_confirm", await _confirm_text(code), event_id="evt_confirm")
    )

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    rows = await _outbox_rows(factory)
    assert entries == 1
    assert pending[0].status == "executed"
    assert pending[0].confirmed_at is not None
    assert pending[0].executed_at is not None
    assert any(row.reply_type == "text" for row in rows)
    # The interpreter was never called for the confirmation message.
    await engine.dispose()


async def test_double_confirm_is_idempotent() -> None:
    engine, factory = await _sqlite_factory()
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.feishu import MessageProcessor

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(),
    )
    await processor.process(
        _message_event("om_c1", await _confirm_text(code), event_id="evt_c1")
    )
    await processor.process(
        _message_event("om_c2", await _confirm_text(code), event_id="evt_c2")
    )

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    assert entries == 1  # business executed exactly once
    pending = await _pending_rows(factory)
    assert pending[0].status == "executed"
    assert len([t for t in feishu.texts if "已确认" in t]) == 1
    await engine.dispose()


async def test_cancel_pending_writes_no_entry() -> None:
    engine, factory = await _sqlite_factory()
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.feishu import MessageProcessor

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(),
    )
    await processor.process(
        _message_event("om_cancel", f"取消 #C-{code[1:]}", event_id="evt_cancel")
    )

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    assert entries == 0
    assert pending[0].status == "cancelled"
    assert pending[0].cancelled_at is not None
    await engine.dispose()


async def test_undo_confirmation_code_cancels_pending_without_touching_ledger() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, short_id="5487J")
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.feishu import MessageProcessor

    interpreter = FixedInterpreter()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        RecordingFeishu(),  # type: ignore[arg-type]
        interpreter,
    )
    await processor.process(
        _message_event("om_cancel", f"撤销 #C-{code[1:]}", event_id="evt_cancel")
    )

    async with factory() as session:
        entry = (await session.scalars(select(LedgerEntry))).one()
    pending = await _pending_rows(factory)
    assert entry.deleted_at is None
    assert pending[0].status == "cancelled"
    assert interpreter.calls == []
    await engine.dispose()


async def test_confirm_after_cancel_fails() -> None:
    engine, factory = await _sqlite_factory()
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.feishu import MessageProcessor

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(),
    )
    await processor.process(
        _message_event("om_cancel", f"取消 #C-{code[1:]}", event_id="evt_cancel")
    )
    await processor.process(
        _message_event("om_confirm", await _confirm_text(code), event_id="evt_confirm")
    )

    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    pending = await _pending_rows(factory)
    assert entries == 0
    assert pending[0].status == "cancelled"
    await engine.dispose()


async def test_expired_pending_cannot_confirm() -> None:
    engine, factory = await _sqlite_factory()
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.pending import PendingCommandStore

    store = PendingCommandStore(factory, Settings(_env_file=None))
    # A now far past the pending's expires_at (created_at + 24h) must refuse.
    far_future = T0 + timedelta(days=400)
    message, rows = await store.confirm_and_execute(
        user_open_id="ou_user",
        confirmation_code=code,
        reply_to_message_id="om_x",
        confirm_event_id="evt_x",
        exchange_rates=None,
        now=far_future,
    )
    assert "已过期" in message
    assert rows[0].payload_json["text"] == message
    pending = await _pending_rows(factory)
    assert pending[0].status == "expired"
    await engine.dispose()


async def test_confirm_requires_user_ownership() -> None:
    engine, factory = await _sqlite_factory()
    code = await _create_pending_via_image(factory)

    from lark_ledger.services.pending import PendingCommandStore

    store = PendingCommandStore(factory, Settings(_env_file=None))
    message, rows = await store.confirm_and_execute(
        user_open_id="ou_other",  # not the owner
        confirmation_code=code,
        reply_to_message_id="om_x",
        confirm_event_id="evt_x",
        exchange_rates=None,
        now=T0,
    )
    assert "未找到" in message
    entries = 0
    async with factory() as session:
        entries = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
    assert entries == 0
    pending = await _pending_rows(factory)
    assert pending[0].status == "pending"
    await engine.dispose()


async def test_list_pending_shows_open_confirmations() -> None:
    engine, factory = await _sqlite_factory()
    await _create_pending_via_image(factory)

    async with factory() as session:
        pending = (await session.execute(select(PendingCommand))).scalar_one()
        # SQLite returns DateTime(timezone=True) values without tzinfo. They are UTC.
        pending.expires_at = datetime(2026, 8, 8, 7, 36)
        await session.commit()

    from lark_ledger.services.feishu import MessageProcessor

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(),
    )
    await processor.process(
        _message_event("om_list", "查看待确认", event_id="evt_list")
    )

    assert any("待确认列表" in text for text in feishu.texts)
    assert any("过期 08-08 15:36" in text for text in feishu.texts)
    assert len(feishu.texts) == 1
    await engine.dispose()
