"""P06a Transactional Outbox through the real MessageProcessor.

Covers the transaction boundary (business + outbox commit together), the crash
window (a committed event converges to ``succeeded`` without re-running
business), delivery semantics (send failures mark the outbox, never the event),
and the compatible post-commit single send for text / CSV / report replies.

Uses SQLite in-memory with injectable interpreters and a recording Feishu
client; no network, no real sleep, no Feishu credentials.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.event_payload import (
    EventProcessStatus,
    build_stored_payload,
    business_event_from_payload,
    parse_stored_payload,
    serialize_payload,
)
from lark_ledger.models import (
    Base,
    Direction,
    LedgerEntry,
    LedgerEntryRevision,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.outbox import ReplyStatus, ReplyType
from lark_ledger.schemas import (
    MAX_EXPORT_ROWS,
    Action,
    EntryCandidate,
    ParsedCommand,
)
from lark_ledger.services.events import EventService
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.worker import EventWorker, EventWorkerStore

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


def _create_command(
    *, amount: str = "32", category: str = "餐饮", note: str | None = None
) -> ParsedCommand:
    return ParsedCommand(
        action=Action.CREATE,
        amount=Decimal(amount),
        direction=Direction.EXPENSE,
        category=category,
        note=note,
        occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
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


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_entry(
    factory: async_sessionmaker[Any],
    *,
    user: str = "ou_user",
    short_id: str = "A83F2",
) -> None:
    async with factory() as session:
        session.add(
            LedgerEntry(
                user_open_id=user,
                short_id=short_id,
                amount=Decimal("10.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()


async def _outbox_rows(factory: async_sessionmaker[Any]) -> list[ReplyOutbox]:
    async with factory() as session:
        rows = (await session.execute(select(ReplyOutbox))).scalars().all()
        return list(rows)


class FixedInterpreter:
    transcription_configured = False
    vision_configured = False

    def __init__(
        self,
        command: ParsedCommand | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self.command = command
        self.exc = exc
        self.calls: list[str] = []

    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes]
    ) -> ParsedCommand:
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        if self.command is None:
            raise AssertionError("interpreter called with no command configured")
        return self.command

    async def generate_advice(self, report: object) -> object:
        raise RuntimeError("AI unavailable (fallback expected)")


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.uploads: list[tuple[bytes, str]] = []
        self.files: list[str] = []
        self.images_uploaded: list[bytes] = []

    async def reply_text(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> None:
        self.cards.append(card)

    async def reply_file(self, message_id: str, file_key: str) -> None:
        self.files.append(file_key)

    async def upload_file(self, content: bytes, filename: str) -> str:
        self.uploads.append((content, filename))
        return "file_key"

    async def upload_image(self, png: bytes) -> str:
        self.images_uploaded.append(png)
        return "image_key"


class FailingReplyFeishu(RecordingFeishu):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    async def reply_text(self, message_id: str, text: str) -> None:
        raise self.exc


class CommittedCheckFeishu(RecordingFeishu):
    """Fails the test if Feishu is called before business + outbox are committed."""

    def __init__(self, factory: async_sessionmaker[Any]) -> None:
        super().__init__()
        self.factory = factory

    async def reply_text(self, message_id: str, text: str) -> None:
        async with self.factory() as session:
            entry = await session.scalar(select(LedgerEntry))
            outbox = await session.scalar(select(ReplyOutbox))
            assert entry is not None, "ledger entry must be committed before any Feishu send"
            assert outbox is not None, "outbox row must be committed before any Feishu send"
        self.texts.append(text)


class StubRenderer:
    def render(self, report: object, advice: object) -> bytes:
        return b"\x89PNG\r\n\x1a\nreport"


# ---------------------------------------------------------------------------
# Transaction atomicity: business + outbox commit together
# ---------------------------------------------------------------------------


async def test_create_entry_and_outbox_commit_atomically() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    await processor.process(_message_event("om_1", "午饭32", event_id="evt_1"))

    async with factory() as session:
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert len(entries) == 1
    assert len(rows) == 1
    assert rows[0].event_id == "evt_1"
    assert rows[0].reply_type == ReplyType.TEXT.value
    assert rows[0].status == ReplyStatus.SENT.value
    assert "已记录" in rows[0].payload_json["text"]
    assert len(feishu.texts) == 1
    await engine.dispose()


async def test_update_entry_and_outbox_commit_atomically() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, short_id="A83F2")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(
            ParsedCommand(action=Action.UPDATE_ENTRY, entry_ref="A83F2", amount=Decimal("35"))
        ),
    )
    await processor.process(_message_event("om_up", "把 #A83F2 改成35元", event_id="evt_up"))

    async with factory() as session:
        entry = (await session.execute(select(LedgerEntry))).scalar_one()
        revision_count = await session.scalar(
            select(func.count()).select_from(LedgerEntryRevision)
        )
    rows = await _outbox_rows(factory)
    assert entry.amount == Decimal("35.00")
    assert revision_count == 1
    assert len(rows) == 1
    assert "已修改 #A83F2" in rows[0].payload_json["text"]
    await engine.dispose()


async def test_delete_and_restore_and_outbox_commit_atomically() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, short_id="A83F2")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(),
    )
    # Deterministic short-ID delete bypasses AI.
    await processor.process(_message_event("om_del", "删除 #A83F2", event_id="evt_del"))
    # Deterministic short-ID restore.
    await processor.process(_message_event("om_res", "恢复 #A83F2", event_id="evt_res"))

    rows = await _outbox_rows(factory)
    assert len(rows) == 2
    assert any("已删除 #A83F2" in row.payload_json["text"] for row in rows)
    assert any("已恢复 #A83F2" in row.payload_json["text"] for row in rows)
    async with factory() as session:
        entry = (await session.execute(select(LedgerEntry))).scalar_one()
    assert entry.deleted_at is None  # restored
    await engine.dispose()


async def test_batch_entries_and_outbox_commit_atomically() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(
            ParsedCommand(
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
        ),
    )
    await processor.process(_message_event("om_b", "早餐10 工资到账20", event_id="evt_b"))

    async with factory() as session:
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert len(entries) == 2
    assert len(rows) == 1
    assert "成功 2 笔" in rows[0].payload_json["text"]
    await engine.dispose()


async def test_outbox_insert_failure_rolls_back_business() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        service = LedgerService(session, commit_changes=False)
        await service.execute(
            "ou_user",
            _create_command(),
        )
        # A reply intent that violates NOT NULL aborts the shared transaction.
        session.add(
            ReplyOutbox(
                event_id="evt_x",
                message_id=None,  # NOT NULL violation
                reply_type=ReplyType.TEXT.value,
                sequence=0,
                transport="feishu",
                payload_version=1,
                payload_json={"text": "x"},
                status=ReplyStatus.PENDING.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with factory() as session:
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    assert entries == []
    await engine.dispose()


async def test_business_failure_leaves_no_outbox() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    interpreter = FixedInterpreter()
    from lark_ledger.services.ai import CommandInterpretationError

    interpreter.exc = CommandInterpretationError("cannot parse")
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        interpreter,
    )
    await processor.process(_message_event("om_err", "???", event_id="evt_err"))

    rows = await _outbox_rows(factory)
    assert rows == []
    assert len(feishu.texts) == 1  # error notice sent directly, outside the outbox
    await engine.dispose()


# ---------------------------------------------------------------------------
# Query / help / file / report replies are durable, not temp-file dependent
# ---------------------------------------------------------------------------


async def test_list_query_reply_persisted_as_stable_text() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, short_id="A83F2")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(ParsedCommand(action=Action.LIST_ENTRIES, limit=10)),
    )
    await processor.process(_message_event("om_list", "最近10笔", event_id="evt_list"))

    rows = await _outbox_rows(factory)
    assert len(rows) == 1
    text = rows[0].payload_json["text"]
    assert "最近 1 笔账目" in text
    assert "#A83F2" in text
    assert text == feishu.texts[0]  # the sent text is the persisted text
    await engine.dispose()


async def test_help_reply_forms_outbox() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(ParsedCommand(action=Action.HELP)),
    )
    await processor.process(_message_event("om_help", "帮助", event_id="evt_help"))

    rows = await _outbox_rows(factory)
    assert len(rows) == 1
    assert rows[0].reply_type == ReplyType.TEXT.value
    assert "记账" in rows[0].payload_json["text"]
    await engine.dispose()


async def test_csv_reply_persists_blob_with_metadata_and_survives_restart() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, short_id="A83F2")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),
    )
    await processor.process(_message_event("om_exp", "导出全部账单", event_id="evt_exp"))

    rows = await _outbox_rows(factory)
    assert len(rows) == 2
    file_row = next(row for row in rows if row.reply_type == ReplyType.FILE.value)
    text_row = next(row for row in rows if row.reply_type == ReplyType.TEXT.value)
    assert text_row.sequence == 1

    # Self-contained: no path, no reference to any temp file.
    meta = file_row.payload_json["file"]
    assert set(meta) == {"filename", "content_type", "size", "sha256"}
    assert "path" not in json.dumps(file_row.payload_json).lower()
    assert file_row.payload_blob is not None
    assert meta["size"] == len(file_row.payload_blob)
    import hashlib

    assert meta["sha256"] == hashlib.sha256(file_row.payload_blob).hexdigest()
    # The exact bytes that Feishu received came from the DB blob.
    assert feishu.uploads and feishu.uploads[0][0] == file_row.payload_blob

    # Restart simulation: a fresh session (as a new worker / process would use)
    # reads the committed blob from the row, not from any temporary file.
    from lark_ledger.services.outbox import ReplyOutboxStore

    reloaded = await ReplyOutboxStore(factory).load_by_ids([file_row.id])
    assert reloaded[0].payload_blob == file_row.payload_blob
    assert file_row.payload_blob.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM CSV
    await engine.dispose()


async def test_export_over_row_limit_rejects_without_file() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        for index in range(MAX_EXPORT_ROWS + 1):
            session.add(
                LedgerEntry(
                    user_open_id="ou_user",
                    short_id=f"S{index:04d}",
                    amount=Decimal("1.00"),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="餐饮",
                    note="",
                    occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
                    source_type="text",
                )
            )
        await session.commit()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),
    )
    await processor.process(_message_event("om_over", "导出全部账单", event_id="evt_over"))

    rows = await _outbox_rows(factory)
    assert len(rows) == 1
    assert rows[0].reply_type == ReplyType.TEXT.value
    assert "超过" in rows[0].payload_json["text"]
    assert feishu.uploads == []
    await engine.dispose()


async def test_report_reply_persists_image_blob_without_temp_path() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_user",
                short_id="RPT01",
                amount=Decimal("32"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(
            ParsedCommand(
                action=Action.REPORT,
                range_start=datetime(2026, 8, 1, tzinfo=UTC),
                range_end=datetime(2026, 9, 1, tzinfo=UTC),
            )
        ),
        renderer=StubRenderer(),  # type: ignore[arg-type]
    )
    await processor.process(_message_event("om_rep", "生成这个月的消费图表", event_id="evt_rep"))

    rows = await _outbox_rows(factory)
    assert len(rows) == 1
    card_row = rows[0]
    assert card_row.reply_type == ReplyType.CARD.value
    assert card_row.payload_blob is not None  # PNG bytes, no temp path
    assert card_row.payload_blob == b"\x89PNG\r\n\x1a\nreport"
    assert card_row.payload_json["image"]["size"] == len(card_row.payload_blob)
    assert "path" not in json.dumps(card_row.payload_json).lower()
    assert len(feishu.cards) == 1
    assert any(el.get("tag") == "img" for el in feishu.cards[0]["body"]["elements"])
    await engine.dispose()


# ---------------------------------------------------------------------------
# Idempotency and the crash window
# ---------------------------------------------------------------------------


async def test_retried_event_does_not_duplicate_business_or_outbox() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    event = _message_event("om_retry", "午饭32", event_id="evt_retry")
    await processor.process(event)
    await processor.process(event)  # simulate a re-claim after a lost status update

    async with factory() as session:
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert len(entries) == 1
    assert len(rows) == 1
    assert len(feishu.texts) == 1  # no re-send of an already-processed event
    await engine.dispose()


async def test_worker_crash_recovery_converges_to_succeeded_not_dead() -> None:
    engine, factory = await _sqlite_factory()
    event_id = "evt_crash"
    payload = serialize_payload(
        build_stored_payload(
            event_id,
            _message_event("om_crash", "午饭32"),
            transport="webhook",
            received_at=T0,
        )
    )
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id=event_id,
                payload_json=payload,
                payload_version=1,
                transport="webhook",
                status=EventProcessStatus.RECEIVED.value,
                received_at=T0,
            )
        )
        await session.commit()

    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    store = EventWorkerStore(factory)
    claimed = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.event_id for item in claimed] == [event_id]

    # The worker's business step commits business + outbox, then we crash before
    # the status update: simulate that by leaving the row processing + leased.
    business_event = business_event_from_payload(parse_stored_payload(payload))
    await processor.process(business_event)
    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None and row.status == EventProcessStatus.PROCESSING.value
        row.lease_expires_at = T0 - timedelta(seconds=1)  # lease expires
        await session.commit()

    # A new worker reclaims the event, re-runs the processor (pre-check skips
    # business) and completes it — it must converge to succeeded, not dead.
    worker = EventWorker(store, processor, owner_id="w2", jitter=None)
    await worker.run_once(now=T0 + timedelta(hours=1))

    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert row is not None
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.attempt_count == 2
    assert len(entries) == 1  # business never re-ran
    assert len(rows) == 1  # outbox never re-inserted
    assert len(feishu.texts) == 1  # reply sent exactly once
    await engine.dispose()


# ---------------------------------------------------------------------------
# Status semantics: send outcome lives on the outbox, never the event
# ---------------------------------------------------------------------------


async def test_send_failure_marks_outbox_failed_but_event_succeeds() -> None:
    engine, factory = await _sqlite_factory()
    event_id = "evt_sendfail"
    payload = serialize_payload(
        build_stored_payload(
            event_id,
            _message_event("om_sf", "午饭32"),
            transport="webhook",
            received_at=T0,
        )
    )
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id=event_id,
                payload_json=payload,
                payload_version=1,
                transport="webhook",
                status=EventProcessStatus.RECEIVED.value,
                received_at=T0,
            )
        )
        await session.commit()

    feishu = FailingReplyFeishu(RuntimeError("feishu reply timeout"))
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    worker = EventWorker(EventWorkerStore(factory), processor, owner_id="w1", jitter=None)
    await worker.run_once(now=T0)

    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert row is not None and row.status == EventProcessStatus.SUCCEEDED.value
    assert len(entries) == 1
    assert len(rows) == 1
    assert rows[0].status == ReplyStatus.FAILED.value
    assert rows[0].last_error_code == "RuntimeError"
    assert rows[0].result_summary == "RuntimeError: feishu reply timeout"
    assert rows[0].attempt_count == 1
    await engine.dispose()


async def test_sync_mode_worker_disabled_writes_outbox_and_succeeds() -> None:
    engine, factory = await _sqlite_factory()
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    service = EventService(factory, processor, worker_enabled=False)
    assert (
        await service.handle(
            "evt_sync_mode", _message_event("om_sm", "午饭32"), transport="webhook"
        )
        is True
    )

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt_sync_mode")
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert row is not None and row.status == EventProcessStatus.SUCCEEDED.value
    assert row.attempt_count == 1
    assert len(entries) == 1
    assert len(rows) == 1
    assert rows[0].status == ReplyStatus.SENT.value
    assert len(feishu.texts) == 1
    await engine.dispose()


async def test_send_failure_does_not_re_execute_business() -> None:
    engine, factory = await _sqlite_factory()
    feishu = FailingReplyFeishu(RuntimeError("down"))
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    event = _message_event("om_again", "午饭32", event_id="evt_again")
    await processor.process(event)
    await processor.process(event)  # retry would only happen for a business error

    async with factory() as session:
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    rows = await _outbox_rows(factory)
    assert len(entries) == 1  # business not re-executed despite a failed send
    assert len(rows) == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# Compatible post-commit single send
# ---------------------------------------------------------------------------


async def test_send_happens_only_after_commit() -> None:
    engine, factory = await _sqlite_factory()
    feishu = CommittedCheckFeishu(factory)
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),
    )
    await processor.process(_message_event("om_order", "午饭32", event_id="evt_order"))

    assert len(feishu.texts) == 1
    await engine.dispose()


async def test_csv_file_and_text_are_both_delivered_synchronously() -> None:
    engine, factory = await _sqlite_factory()
    await _seed_entry(factory, short_id="A83F2")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),
    )
    await processor.process(_message_event("om_csv", "导出全部账单", event_id="evt_csv"))

    assert feishu.files == ["file_key"]
    assert feishu.uploads and feishu.uploads[0][1].endswith(".csv")
    assert any("已导出" in text for text in feishu.texts)
    await engine.dispose()
