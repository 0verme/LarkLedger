import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lark_ledger.config import Settings
from lark_ledger.event_payload import (
    EventProcessStatus,
    parse_stored_payload,
)
from lark_ledger.models import Direction, LedgerEntry, ProcessedEvent
from lark_ledger.readiness import ReadinessService
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.events import EventService
from lark_ledger.services.ledger import LedgerService

pytestmark = pytest.mark.postgres

# Resolved outside async tests so pathlib is not used inside event loops.
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def process(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _message_event(message_id: str) -> dict[str, Any]:
    return {
        "sender": {"sender_id": {"open_id": "ou_integration"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": '{"text":"integration"}',
        },
    }


async def test_alembic_schema_is_at_head(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "20260806_0010"


async def test_readiness_uses_real_postgres_and_current_alembic_revision(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
    )
    service = ReadinessService(settings, postgres_session_factory)

    class State:
        shutting_down = False

    result = await service.check(State())  # type: ignore[arg-type]

    assert result["status"] == "ready"
    assert result["checks"]["database"] == {"status": "ok"}
    assert result["checks"]["migration"] == {
        "status": "ok",
        "current": "20260806_0010",
        "expected": "20260806_0010",
    }


async def test_list_keyset_and_get_entry_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    when = datetime(2026, 8, 5, 12, tzinfo=UTC)
    async with postgres_session_factory() as session:
        for index, code in enumerate(["PG001", "PG002", "PG003"]):
            entry = LedgerEntry(
                user_open_id="ou_pg",
                short_id=code,
                amount=Decimal(str(index + 1)),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=when,
                source_type="text",
            )
            session.add(entry)
            await session.flush()
            entry.created_at = when + timedelta(seconds=index)
            entry.updated_at = entry.created_at
        await session.commit()

        service = LedgerService(session)
        page1 = await service.execute(
            "ou_pg", ParsedCommand(action=Action.LIST_ENTRIES, limit=2)
        )
        # Newest by created_at: PG003, PG002
        assert "1. #PG003" in page1.message
        assert "2. #PG002" in page1.message
        assert "查看 #PG002 之前的2笔" in page1.message
        page2 = await service.execute(
            "ou_pg",
            ParsedCommand(
                action=Action.LIST_ENTRIES,
                limit=2,
                before_entry_ref="PG002",
            ),
        )
        assert "最近 1 笔账目" in page2.message
        assert "#PG001" in page2.message
        detail = await service.execute(
            "ou_pg", ParsedCommand(action=Action.GET_ENTRY, entry_ref="PG001")
        )
        assert "短 ID：#PG001" in detail.message
        assert "ou_pg" not in detail.message


async def test_entry_mutation_and_revision_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from lark_ledger.models import LedgerEntryRevision

    async with postgres_session_factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_mut",
                short_id="MT01A",
                amount=Decimal("10.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="x",
                occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()
        service = LedgerService(session)
        updated = await service.execute(
            "ou_mut",
            ParsedCommand(
                action=Action.UPDATE_ENTRY,
                entry_ref="MT01A",
                amount=Decimal("12.00"),
            ),
        )
        assert "已修改 #MT01A" in updated.message
        deleted = await service.execute(
            "ou_mut", ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="MT01A")
        )
        assert "已删除 #MT01A" in deleted.message
        restored = await service.execute(
            "ou_mut", ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref="MT01A")
        )
        assert "已恢复 #MT01A" in restored.message
        count = await session.scalar(select(func.count()).select_from(LedgerEntryRevision))
        assert count == 3
        entry = (
            await session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.user_open_id == "ou_mut",
                    LedgerEntry.short_id == "MT01A",
                )
            )
        ).scalar_one()
        assert entry.amount == Decimal("12.00")
        assert entry.deleted_at is None
        assert entry.short_id == "MT01A"


async def test_export_entries_query_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P04: export query, isolation, half-open range, deleted filter, stable sort."""
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 7, 1, tzinfo=UTC)
    mid = datetime(2026, 6, 15, 8, tzinfo=UTC)
    async with postgres_session_factory() as session:
        for index, (user, code, deleted, when) in enumerate(
            [
                ("ou_export_a", "EX001", False, mid),
                ("ou_export_a", "EX002", True, mid + timedelta(hours=1)),
                ("ou_export_a", "EX003", False, mid + timedelta(hours=2)),
                ("ou_export_b", "EX001", False, mid),
                ("ou_export_a", "EXAGE", False, datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        ):
            entry = LedgerEntry(
                user_open_id=user,
                short_id=code,
                amount=Decimal(str(index + 1)),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮" if user == "ou_export_a" else "他户",
                note="secret-b" if user == "ou_export_b" else "",
                occurred_at=when,
                source_type="text",
                deleted_at=when if deleted else None,
            )
            session.add(entry)
            await session.flush()
            entry.created_at = when + timedelta(seconds=index)
            entry.updated_at = entry.created_at
        await session.commit()

        service = LedgerService(session)
        default = await service.execute(
            "ou_export_a",
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=start,
                range_end=end,
            ),
        )
        assert default.export is not None
        body = default.export.content.decode("utf-8-sig")
        assert "#EX001" in body
        assert "#EX003" in body
        assert "#EX002" not in body
        assert "#EXAGE" not in body
        assert "他户" not in body
        assert "secret-b" not in body
        assert "ou_export" not in body
        rows = [line for line in body.splitlines() if line.startswith("#")]
        assert rows[0].startswith("#EX001")
        assert rows[1].startswith("#EX003")

        with_deleted = await service.execute(
            "ou_export_a",
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=start,
                range_end=end,
                include_deleted=True,
            ),
        )
        assert with_deleted.export is not None
        assert with_deleted.export.row_count == 3
        assert "#EX002" in with_deleted.export.content.decode("utf-8-sig")

        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "20260806_0010"


async def test_short_id_unique_per_user_allows_cross_user_reuse(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_a",
                short_id="A83F2",
                amount=Decimal("1.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()

        session.add(
            LedgerEntry(
                user_open_id="ou_a",
                short_id="A83F2",
                amount=Decimal("2.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="交通",
                note="",
                occurred_at=datetime(2026, 8, 5, 13, tzinfo=UTC),
                source_type="text",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(
            LedgerEntry(
                user_open_id="ou_b",
                short_id="A83F2",
                amount=Decimal("3.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="购物",
                note="",
                occurred_at=datetime(2026, 8, 5, 14, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(LedgerEntry))
        assert count == 2


async def test_concurrent_event_claim_is_processed_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    processor = RecordingProcessor()
    service = EventService(postgres_session_factory, processor)
    event = _message_event("om_concurrent")

    results = await asyncio.gather(
        *(service.handle("evt_concurrent", event, transport="webhook") for _ in range(8))
    )

    assert sum(results) == 1
    assert len(processor.events) == 1
    assert processor.events[0]["message"]["message_id"] == "om_concurrent"

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, "evt_concurrent")
        assert row is not None
        assert row.payload_json is not None
        assert row.payload_version == 1
        assert row.transport == "webhook"
        assert row.status == EventProcessStatus.SUCCEEDED.value
        parsed = parse_stored_payload(row.payload_json)
        assert parsed["event"]["message"]["message_id"] == "om_concurrent"


def make_entry(source_item_index: int, short_id: str | None = None) -> LedgerEntry:
    # Distinct Crockford codes for integration fixtures (no I/L/O/U).
    defaults = ("AAAAA", "AAAAB", "AAAAC", "AAAAD", "AAAAE")
    return LedgerEntry(
        user_open_id="ou_integration",
        short_id=short_id or defaults[source_item_index],
        amount=Decimal("12.34"),
        currency="CNY",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="integration test",
        occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        source_type="text",
        source_message_id="om_batch",
        source_item_index=source_item_index,
    )


async def test_batch_source_constraint_allows_distinct_items_only(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add_all([make_entry(0), make_entry(1)])
        await session.commit()
        session.add(make_entry(1))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        count = await session.scalar(select(func.count()).select_from(LedgerEntry))

    assert count == 2


async def test_transaction_can_continue_after_unique_violation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add(
            ProcessedEvent(
                event_id="evt_existing",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
            )
        )
        await session.commit()

        session.add(
            ProcessedEvent(
                event_id="evt_existing",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(
            ProcessedEvent(
                event_id="evt_after_rollback",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
            )
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(ProcessedEvent))
        legacy = await session.get(ProcessedEvent, "evt_existing")
        assert legacy is not None
        assert legacy.payload_json is None
        assert legacy.status == EventProcessStatus.LEGACY_SUCCEEDED.value

    assert count == 2


async def test_event_service_records_reliability_state_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    processor = RecordingProcessor()
    service = EventService(postgres_session_factory, processor)
    event = _message_event("om_state")
    assert await service.handle("evt_state", event, transport="webhook")

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, "evt_state")
        assert row is not None
        assert row.status == EventProcessStatus.SUCCEEDED.value
        assert row.attempt_count == 1
        assert row.source_message_id == "om_state"
        assert row.user_open_id == "ou_integration"
        assert row.result_summary is None
        assert row.next_attempt_at is None
        assert row.lease_owner is None
        assert row.lease_expires_at is None
        # Production storage must return timezone-aware timestamps.
        assert row.received_at is not None and row.received_at.tzinfo is not None
        assert row.updated_at is not None and row.updated_at.tzinfo is not None


async def test_event_service_records_failure_summary_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class FailingProcessor:
        async def process(self, event: dict[str, Any]) -> None:
            raise RuntimeError("pg boom")

    service = EventService(postgres_session_factory, FailingProcessor())
    with pytest.raises(RuntimeError, match="pg boom"):
        await service.handle(
            "evt_state_fail", _message_event("om_state_fail"), transport="websocket"
        )

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, "evt_state_fail")
        assert row is not None
        assert row.status == EventProcessStatus.FAILED.value
        assert row.attempt_count == 1
        assert row.last_error_code == "RuntimeError"
        assert row.result_summary == "RuntimeError: pg boom"
        assert row.updated_at.tzinfo is not None


@pytest.mark.postgres
async def test_event_state_migration_roundtrip(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade 0006 -> 0007 backfills state, then downgrade drops it cleanly.

    Runs against a scratch database so the shared test DB and other tests are
    unaffected. Alembic runs in a worker thread because env.py calls
    ``asyncio.run`` (a thread gets its own event loop).
    """
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

    seed_payload = json.dumps(
        {
            "payload_version": 1,
            "event_id": "evt_success",
            "transport": "webhook",
            "received_at": "2026-08-06T00:00:00+00:00",
            "event": {
                "sender": {"sender_id": {"open_id": "ou_mig"}},
                "message": {"message_id": "om_mig", "message_type": "text", "content": "{}"},
            },
        },
        ensure_ascii=False,
    )
    failed_payload = json.dumps(
        {
            "payload_version": 1,
            "event_id": "evt_failed",
            "transport": "websocket",
            "received_at": "2026-08-06T00:00:00+00:00",
            "event": {
                "sender": {"sender_id": {"open_id": "ou_fail"}},
                "message": {"message_id": "om_fail", "message_type": "text", "content": "{}"},
            },
        },
        ensure_ascii=False,
    )

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        # CREATE / DROP DATABASE cannot run inside a transaction block.
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(_run_migrations, "20260805_0006")

        # Seed pre-0007 rows exactly as v0.2.0 would have left them. begin()
        # commits on exit; engine.connect() would roll the inserts back.
        async with scratch_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO processed_events "
                    "(event_id, payload_json, payload_version, transport, status, processed_at) "
                    "VALUES ('evt_legacy', NULL, NULL, NULL, 'legacy_succeeded', now())"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO processed_events "
                    "(event_id, payload_json, payload_version, transport, status, processed_at) "
                    "VALUES ('evt_success', CAST(:payload AS json), "
                    "1, 'webhook', 'succeeded', now())"
                ),
                {"payload": seed_payload},
            )
            await conn.execute(
                text(
                    "INSERT INTO processed_events "
                    "(event_id, payload_json, payload_version, transport, status, processed_at) "
                    "VALUES ('evt_failed', CAST(:payload AS json), 1, 'websocket', 'failed', now())"
                ),
                {"payload": failed_payload},
            )
            await conn.execute(
                text(
                    "INSERT INTO processed_events (event_id, status, processed_at) "
                    "VALUES ('evt_received', 'received', now())"
                )
            )

        # Apply 0007.
        await asyncio.to_thread(_run_migrations, "head")

        async with scratch_engine.connect() as conn:
            legacy = (
                await conn.execute(
                    text(
                        "SELECT status, attempt_count, source_message_id, user_open_id, "
                        "updated_at, payload_json FROM processed_events "
                        "WHERE event_id = 'evt_legacy'"
                    )
                )
            ).one()
            assert legacy.status == "legacy_succeeded"
            assert legacy.attempt_count == 0
            assert legacy.source_message_id is None
            assert legacy.user_open_id is None
            assert legacy.payload_json is None  # legacy stays non-replayable
            assert legacy.updated_at is not None

            success = (
                await conn.execute(
                    text(
                        "SELECT status, attempt_count, source_message_id, user_open_id, "
                        "updated_at FROM processed_events WHERE event_id = 'evt_success'"
                    )
                )
            ).one()
            assert success.status == "succeeded"
            assert success.attempt_count == 1
            assert success.source_message_id == "om_mig"
            assert success.user_open_id == "ou_mig"
            assert success.updated_at is not None

            failed = (
                await conn.execute(
                    text(
                        "SELECT attempt_count, source_message_id, user_open_id "
                        "FROM processed_events WHERE event_id = 'evt_failed'"
                    )
                )
            ).one()
            assert failed.attempt_count == 1
            assert failed.source_message_id == "om_fail"
            assert failed.user_open_id == "ou_fail"

            received = (
                await conn.execute(
                    text(
                        "SELECT attempt_count FROM processed_events "
                        "WHERE event_id = 'evt_received'"
                    )
                )
            ).one()
            assert received.attempt_count == 0

            # Server defaults apply to rows inserted directly via SQL.
            await conn.execute(
                text(
                    "INSERT INTO processed_events (event_id, status) "
                    "VALUES ('evt_fresh', 'received')"
                )
            )
            fresh = (
                await conn.execute(
                    text(
                        "SELECT status, attempt_count, updated_at FROM processed_events "
                        "WHERE event_id = 'evt_fresh'"
                    )
                )
            ).one()
            assert fresh.status == "received"
            assert fresh.attempt_count == 0
            assert fresh.updated_at is not None

        # Downgrade to 0006: new columns disappear, retained data preserved.
        await asyncio.to_thread(_run_downgrade, "20260805_0006")
        async with scratch_engine.connect() as conn:
            new_columns = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = 'processed_events' AND column_name IN "
                    "('attempt_count','next_attempt_at','lease_owner','lease_expires_at',"
                    "'result_summary','source_message_id','user_open_id','updated_at')"
                )
            )
            assert new_columns == 0
            kept = (
                await conn.execute(
                    text(
                        "SELECT status, payload_json FROM processed_events "
                        "WHERE event_id = 'evt_success'"
                    )
                )
            ).one()
            assert kept.status == "succeeded"
            assert kept.payload_json is not None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        # Close pooled connections to the scratch database before dropping it.
        await scratch_engine.dispose()
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
