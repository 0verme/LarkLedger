"""P07 PostgreSQL integration: pending confirmations on real storage.

Covers the ``20260806_0012`` migration roundtrip and the real
``FOR UPDATE`` concurrency semantics: two confirms execute the frozen command
exactly once, and a concurrent confirm vs cancel has exactly one winner. The
schema is created by ``alembic upgrade head``; the ``postgres_engine`` fixture
truncates all tables between tests.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lark_ledger.config import Settings
from lark_ledger.models import (
    Direction,
    LedgerEntry,
    PendingCommand,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.pending import PendingCommandStore

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


def _frozen_create() -> dict[str, Any]:
    return ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32.00"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=T0,
    ).model_dump(mode="json")


async def _seed_pending(
    factory: async_sessionmaker[Any],
    *,
    code: str = "CAAAA1",
    user: str = "ou_user",
) -> None:
    async with factory() as session:
        session.add(
            PendingCommand(
                confirmation_code=code,
                user_open_id=user,
                source_type="text",
                command_type="create",
                payload_version=1,
                payload_json=_frozen_create(),
                preview_json={"code": code},
                risk_reason="duplicate",
                status="pending",
                expires_at=T0 + timedelta(days=1),
            )
        )
        await session.commit()


async def _entry_count(factory: async_sessionmaker[Any]) -> int:
    async with factory() as session:
        return int(
            (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar()
        )


def _store(factory: async_sessionmaker[Any]) -> PendingCommandStore:
    return PendingCommandStore(factory, Settings(_env_file=None))


async def test_two_concurrent_confirms_execute_exactly_once(
    postgres_engine: AsyncEngine, postgres_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_pending(postgres_session_factory)
    store = _store(postgres_session_factory)

    results = await asyncio.gather(
        store.confirm_and_execute(
            user_open_id="ou_user",
            confirmation_code="CAAAA1",
            reply_to_message_id="om_a",
            confirm_event_id=None,
            exchange_rates=None,
            now=T0,
        ),
        store.confirm_and_execute(
            user_open_id="ou_user",
            confirmation_code="CAAAA1",
            reply_to_message_id="om_b",
            confirm_event_id=None,
            exchange_rates=None,
            now=T0,
        ),
        return_exceptions=True,
    )
    assert all(isinstance(result, tuple) for result in results), results
    messages = [result[0] for result in results]

    # Exactly one confirm executed the business; the other is idempotent.
    executed = [m for m in messages if "无需重复操作" not in m]
    idempotent = [m for m in messages if "无需重复操作" in m]
    assert len(executed) == 1
    assert len(idempotent) == 1
    assert await _entry_count(postgres_session_factory) == 1

    async with postgres_session_factory() as session:
        row = (await session.scalars(select(PendingCommand))).one()
    assert row.status == "executed"


async def test_concurrent_confirm_and_cancel_have_one_winner(
    postgres_engine: AsyncEngine, postgres_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_pending(postgres_session_factory)
    store = _store(postgres_session_factory)

    results = await asyncio.gather(
        store.confirm_and_execute(
            user_open_id="ou_user",
            confirmation_code="CAAAA1",
            reply_to_message_id="om_c",
            confirm_event_id=None,
            exchange_rates=None,
            now=T0,
        ),
        store.cancel(
            user_open_id="ou_user",
            confirmation_code="CAAAA1",
            reply_to_message_id="om_x",
            cancel_event_id=None,
            now=T0,
        ),
        return_exceptions=True,
    )
    assert all(isinstance(result, tuple) for result in results), results

    async with postgres_session_factory() as session:
        row = (await session.scalars(select(PendingCommand))).one()
    entries = await _entry_count(postgres_session_factory)

    if row.status == "executed":
        assert entries == 1
    elif row.status == "cancelled":
        assert entries == 0
    else:
        pytest.fail(f"unexpected terminal status: {row.status}")


async def test_confirm_directive_event_sets_business_committed_at(
    postgres_engine: AsyncEngine, postgres_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import json

    from lark_ledger.services.feishu import MessageProcessor

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

    class Interpreter:
        transcription_configured = False
        vision_configured = True

        async def interpret(self, text, *, now, images):
            return ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("32.00"),
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
                occurred_at=T0,
            )

    processor = MessageProcessor(
        Settings(_env_file=None),
        postgres_session_factory,
        RecordingFeishu(),  # type: ignore[arg-type]
        Interpreter(),  # type: ignore[arg-type]
    )
    # The event worker claims events (creating processed_events rows) before
    # processing; the outbox FK requires them on real Postgres.
    async with postgres_session_factory() as session:
        session.add_all(
            [
                ProcessedEvent(event_id="evt_img", status="received"),
                ProcessedEvent(event_id="evt_confirm", status="received"),
            ]
        )
        await session.commit()
    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_img",
                "message_type": "image",
                "content": json.dumps({"image_key": "img_1"}),
            },
            "event_id": "evt_img",
        }
    )
    async with postgres_session_factory() as session:
        code = (await session.scalars(select(PendingCommand))).one().confirmation_code

    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_confirm",
                "message_type": "text",
                "content": json.dumps(
                    {"text": f"确认 #C-{code[1:]}"}, ensure_ascii=False
                ),
            },
            "event_id": "evt_confirm",
        }
    )

    async with postgres_session_factory() as session:
        confirm_event = await session.get(ProcessedEvent, "evt_confirm")
        pending = (await session.scalars(select(PendingCommand))).one()
        entry = (await session.scalars(select(LedgerEntry))).one()
    assert confirm_event is not None
    assert confirm_event.business_committed_at is not None
    assert pending.status == "executed"
    assert entry.amount == Decimal("32.00")
    assert entry.category == "餐饮"


async def test_concurrent_identical_images_create_one_pending(
    postgres_engine: AsyncEngine, postgres_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import json

    from lark_ledger.services.feishu import MessageProcessor

    class RecordingFeishu:
        async def reply_text(self, message_id, text, *, uuid=None):
            return None

        async def reply_card(self, message_id, card, *, uuid=None):
            return None

        async def reply_file(self, message_id, file_key, *, uuid=None):
            return None

        async def upload_file(self, content, filename):
            return "file_key"

        async def upload_image(self, png):
            return "image_key"

        async def download_resource(self, message_id, file_key, kind):
            return b"exact-same-image"

    class Interpreter:
        transcription_configured = False
        vision_configured = True

        async def interpret(self, text, *, now, images):
            return ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("32.00"),
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
                occurred_at=T0,
            )

    processor = MessageProcessor(
        Settings(_env_file=None),
        postgres_session_factory,
        RecordingFeishu(),  # type: ignore[arg-type]
        Interpreter(),  # type: ignore[arg-type]
    )
    async with postgres_session_factory() as session:
        session.add_all(
            [
                ProcessedEvent(event_id="evt_same_1", status="received"),
                ProcessedEvent(event_id="evt_same_2", status="received"),
            ]
        )
        await session.commit()

    def image_event(event_id: str, message_id: str) -> dict[str, Any]:
        return {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": message_id,
                "message_type": "image",
                "content": json.dumps({"image_key": "img_same"}),
            },
            "event_id": event_id,
        }

    await asyncio.gather(
        processor.process(image_event("evt_same_1", "om_same_1")),
        processor.process(image_event("evt_same_2", "om_same_2")),
    )

    async with postgres_session_factory() as session:
        pending_count = await session.scalar(
            select(func.count()).select_from(PendingCommand)
        )
        outbox_count = await session.scalar(select(func.count()).select_from(ReplyOutbox))
        events = (
            await session.scalars(
                select(ProcessedEvent).where(
                    ProcessedEvent.event_id.in_(["evt_same_1", "evt_same_2"])
                )
            )
        ).all()
    assert pending_count == 1
    assert outbox_count == 1
    assert all(event.business_committed_at is not None for event in events)


async def test_pending_migration_roundtrip_0012_and_0013(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0012 creates pending_commands; 0013 adds and removes media dedupe."""
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_pending_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    base_dsn = url.render_as_string(hide_password=False)

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "20260806_0012")

        async with scratch_engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20260806_0012"
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_commands' ORDER BY column_name"
                )
            )
            names = {row[0] for row in columns}
            for expected in (
                "confirmation_code",
                "user_open_id",
                "payload_json",
                "preview_json",
                "risk_reason",
                "status",
                "expires_at",
            ):
                assert expected in names, f"pending_commands missing column {expected}"

        await asyncio.to_thread(
            command.upgrade, Config(str(_ALEMBIC_INI)), "20260807_0013"
        )
        async with scratch_engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20260807_0013"
            fingerprint_column = await conn.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_commands' "
                    "AND column_name = 'source_fingerprint'"
                )
            )
            fingerprint_index = await conn.scalar(
                text("SELECT to_regclass('public.uq_pending_user_active_fingerprint')")
            )
            assert fingerprint_column == "source_fingerprint"
            assert fingerprint_index == "uq_pending_user_active_fingerprint"

        await asyncio.to_thread(
            command.downgrade, Config(str(_ALEMBIC_INI)), "20260806_0012"
        )
        async with scratch_engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            fingerprint_column = await conn.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_commands' "
                    "AND column_name = 'source_fingerprint'"
                )
            )
            assert revision == "20260806_0012"
            assert fingerprint_column is None

        await asyncio.to_thread(
            command.downgrade, Config(str(_ALEMBIC_INI)), "20260806_0011"
        )
        async with scratch_engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20260806_0011"
            table = await conn.scalar(
                text(
                    "SELECT to_regclass('public.pending_commands') "
                )
            )
            assert table is None, "pending_commands should be dropped on downgrade"
    finally:
        await scratch_engine.dispose()
        await maint_engine.dispose()
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
