"""P06a PostgreSQL integration: transactional reply outbox.

Exercises the atomic business + outbox transaction, the crash-window
convergence (a committed event re-claimed after a lost status update ends
``succeeded``, never ``dead``), the unique ``(event_id, reply_type)``
idempotency guard, send-failure semantics on real storage, and the
``20260806_0008`` migration roundtrip. The schema is created by
``alembic upgrade head`` (CI runs it before this suite); the
``postgres_engine`` fixture truncates all tables between tests.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lark_ledger.config import Settings
from lark_ledger.event_payload import (
    EventProcessStatus,
    build_stored_payload,
    business_event_from_payload,
    parse_stored_payload,
    serialize_payload,
)
from lark_ledger.models import Direction, LedgerEntry, ProcessedEvent, ReplyOutbox
from lark_ledger.outbox import ReplyStatus, ReplyType
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.feishu import MessageProcessor
from lark_ledger.services.worker import EventWorker, EventWorkerStore

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _create_command() -> ParsedCommand:
    return ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32"),
        direction=Direction.EXPENSE,
        category="餐饮",
        occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
    )


def _message_event(message_id: str) -> dict[str, Any]:
    return {
        "sender": {"sender_id": {"open_id": "ou_pg_outbox"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": '{"text":"午饭32"}',
        },
    }


def _payload(event_id: str, message_id: str) -> dict[str, Any]:
    return serialize_payload(
        build_stored_payload(
            event_id,
            _message_event(message_id),
            transport="webhook",
            received_at=T0,
        )
    )


class FixedInterpreter:
    transcription_configured = False
    vision_configured = False

    def __init__(self, command: ParsedCommand) -> None:
        self.command = command

    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes]
    ) -> ParsedCommand:
        return self.command

    async def generate_advice(self, report: object) -> object:
        raise RuntimeError("AI unavailable (fallback expected)")


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(
        self, message_id: str, text: str, *, uuid: str | None = None
    ) -> None:
        self.texts.append(text)


class FailingReplyFeishu(RecordingFeishu):
    async def reply_text(
        self, message_id: str, text: str, *, uuid: str | None = None
    ) -> None:
        raise RuntimeError("feishu reply timeout")


async def _insert_event(
    factory: async_sessionmaker[Any],
    event_id: str,
    *,
    message_id: str,
    status: str = EventProcessStatus.RECEIVED.value,
) -> None:
    async with factory() as session:
        payload = _payload(event_id, message_id)
        session.add(
            ProcessedEvent(
                event_id=event_id,
                payload_json=payload,
                payload_version=1,
                transport="webhook",
                status=status,
                received_at=T0,
            )
        )
        await session.commit()


async def test_business_and_outbox_commit_atomically_on_postgres(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert_event(postgres_session_factory, "evt_pg_atomic", message_id="om_pg_atomic")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        postgres_session_factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),  # type: ignore[arg-type]
    )
    worker = EventWorker(
        EventWorkerStore(postgres_session_factory), processor, owner_id="w1", jitter=None
    )
    await worker.run_once(now=T0)

    async with postgres_session_factory() as session:
        event = await session.get(ProcessedEvent, "evt_pg_atomic")
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
        outbox = (await session.execute(select(ReplyOutbox))).scalars().all()
    assert event is not None and event.status == EventProcessStatus.SUCCEEDED.value
    assert len(entries) == 1
    assert len(outbox) == 1
    assert outbox[0].event_id == "evt_pg_atomic"
    assert outbox[0].status == ReplyStatus.SENT.value


async def test_outbox_unique_constraint_prevents_duplicate_reply(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert_event(postgres_session_factory, "evt_uniq_pg", message_id="om_u")
    async with postgres_session_factory() as session:
        session.add(
            ReplyOutbox(
                event_id="evt_uniq_pg",
                message_id="om_u",
                reply_type=ReplyType.TEXT.value,
                sequence=0,
                transport="feishu",
                payload_version=1,
                payload_json={"text": "first"},
                status=ReplyStatus.PENDING.value,
            )
        )
        await session.commit()
        session.add(
            ReplyOutbox(
                event_id="evt_uniq_pg",
                message_id="om_u",
                reply_type=ReplyType.TEXT.value,
                sequence=1,
                transport="feishu",
                payload_version=1,
                payload_json={"text": "second"},
                status=ReplyStatus.PENDING.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_crash_recovery_converges_to_succeeded_not_dead_on_postgres(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    event_id = "evt_pg_crash"
    await _insert_event(postgres_session_factory, event_id, message_id="om_pg_crash")
    feishu = RecordingFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        postgres_session_factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),  # type: ignore[arg-type]
    )
    store = EventWorkerStore(postgres_session_factory)
    claimed = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.event_id for item in claimed] == [event_id]

    # Business + outbox commit, then a crash before the status update: the row
    # stays processing + leased.
    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        payload = row.payload_json if row is not None else None
    assert payload is not None
    await processor.process(business_event_from_payload(parse_stored_payload(payload)))

    # Expire the lease to simulate the crash window.
    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None
        row.lease_expires_at = T0 - timedelta(seconds=1)
        await session.commit()

    worker = EventWorker(store, processor, owner_id="w2", jitter=None)
    await worker.run_once(now=T0 + timedelta(hours=1))

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
        outbox = (await session.execute(select(ReplyOutbox))).scalars().all()
    assert row is not None
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.last_error_code is None
    assert len(entries) == 1  # business never re-ran
    assert len(outbox) == 1  # outbox never re-inserted
    assert len(feishu.texts) == 1


async def test_send_failure_marks_outbox_failed_event_succeeds_on_postgres(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    event_id = "evt_pg_sendfail"
    await _insert_event(postgres_session_factory, event_id, message_id="om_pg_sf")
    feishu = FailingReplyFeishu()
    processor = MessageProcessor(
        Settings(_env_file=None),
        postgres_session_factory,
        feishu,  # type: ignore[arg-type]
        FixedInterpreter(_create_command()),  # type: ignore[arg-type]
    )
    worker = EventWorker(
        EventWorkerStore(postgres_session_factory), processor, owner_id="w1", jitter=None
    )
    await worker.run_once(now=T0)

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        outbox = (await session.execute(select(ReplyOutbox))).scalars().all()
    assert row is not None and row.status == EventProcessStatus.SUCCEEDED.value
    assert len(outbox) == 1
    assert outbox[0].status == ReplyStatus.FAILED.value
    assert outbox[0].last_error_code == "RuntimeError"


@pytest.mark.postgres
async def test_outbox_migration_roundtrip_0008(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade to 0008 adds reply_outbox; downgrade to 0007 drops it."""
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    base_dsn = url.render_as_string(hide_password=False)

    def _run_migrations(target: str) -> None:
        command.upgrade(Config(str(_ALEMBIC_INI)), target)

    def _run_downgrade(target: str) -> None:
        command.downgrade(Config(str(_ALEMBIC_INI)), target)

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(_run_migrations, "20260806_0007")
        await asyncio.to_thread(_run_migrations, "20260806_0008")

        async with scratch_engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20260806_0008"
            table = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = 'reply_outbox'"
                )
            )
            assert table == 1
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'reply_outbox' ORDER BY column_name"
                )
            )
            names = {row[0] for row in columns}
            for expected in (
                "id",
                "event_id",
                "message_id",
                "reply_type",
                "sequence",
                "transport",
                "payload_version",
                "payload_json",
                "payload_blob",
                "status",
                "attempt_count",
                "next_attempt_at",
                "lease_owner",
                "lease_expires_at",
                "sent_at",
                "last_error_code",
                "result_summary",
                "created_at",
                "updated_at",
            ):
                assert expected in names, f"reply_outbox missing column {expected}"

        await asyncio.to_thread(_run_downgrade, "20260806_0007")
        async with scratch_engine.connect() as conn:
            table = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name = 'reply_outbox'"
                )
            )
            assert table == 0
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
