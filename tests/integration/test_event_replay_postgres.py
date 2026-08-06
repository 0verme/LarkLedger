"""P06e PostgreSQL locking and migration coverage for manual event replay."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from lark_ledger.config import get_settings
from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    REPLAY_SAFETY_VERSION,
    EventProcessStatus,
    build_stored_payload,
)
from lark_ledger.models import EventReplayAudit, ProcessedEvent
from lark_ledger.services.event_replay import EventReplayService
from lark_ledger.services.worker import EventWorkerStore

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def replayable_event(event_id: str) -> ProcessedEvent:
    message_id = f"message-{event_id}"
    payload = build_stored_payload(
        event_id,
        {
            "sender": {"sender_id": {"open_id": "integration-user"}},
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": "integration private content"}),
            },
        },
        transport="webhook",
        received_at=NOW - timedelta(minutes=5),
    )
    return ProcessedEvent(
        event_id=event_id,
        payload_json=payload,
        payload_version=PAYLOAD_VERSION,
        replay_safety_version=REPLAY_SAFETY_VERSION,
        status=EventProcessStatus.DEAD.value,
        attempt_count=3,
        manual_replay_count=0,
        source_message_id=message_id,
        user_open_id="integration-user",
        last_error_code="TemporaryFailure",
        result_summary="safe summary",
        updated_at=NOW - timedelta(minutes=1),
    )


async def test_two_operators_concurrently_replay_only_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add(replayable_event("evt-replay-concurrent"))
        await session.commit()

    service = EventReplayService(postgres_session_factory)
    first, second = await asyncio.gather(
        service.replay(
            "evt-replay-concurrent",
            operator="operator-a",
            reason="dependency recovered",
            execute=True,
            now=NOW,
        ),
        service.replay(
            "evt-replay-concurrent",
            operator="operator-b",
            reason="dependency recovered",
            execute=True,
            now=NOW,
        ),
    )

    assert sorted([first.outcome, second.outcome]) == ["rejected", "requeued"]
    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, "evt-replay-concurrent")
        audits = int(
            await session.scalar(select(func.count()).select_from(EventReplayAudit)) or 0
        )
        assert row is not None
        assert row.status == EventProcessStatus.RECEIVED.value
        assert row.manual_replay_count == 1
        assert row.attempt_count == 0
        assert audits == 2


async def test_worker_and_manual_replay_race_preserves_one_owner(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = "evt-replay-worker-race"
    async with postgres_session_factory() as session:
        session.add(replayable_event(event_id))
        await session.commit()

    replay_result, claims = await asyncio.gather(
        EventReplayService(postgres_session_factory).replay(
            event_id,
            operator="operator",
            reason="dependency recovered",
            execute=True,
            now=NOW,
        ),
        EventWorkerStore(postgres_session_factory).claim_batch(
            "worker-race", NOW, batch_size=1, lease_seconds=60
        ),
    )

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None
        if claims:
            assert claims[0].event_id == event_id
            assert row.status == EventProcessStatus.PROCESSING.value
            assert row.lease_owner == "worker-race"
            assert replay_result.outcome in {"requeued", "rejected"}
        else:
            assert replay_result.outcome == "requeued"
            assert row.status == EventProcessStatus.RECEIVED.value
            assert row.lease_owner is None


async def test_guarded_replay_migration_upgrade_downgrade_roundtrip(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = make_url(postgres_url)
    base_dsn = url.set(database="postgres").render_as_string(hide_password=False)
    scratch = f"lark_ledger_replay_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)

    def upgrade(target: str) -> None:
        command.upgrade(Config(str(_ALEMBIC_INI)), target)

    def downgrade(target: str) -> None:
        command.downgrade(Config(str(_ALEMBIC_INI)), target)

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(upgrade, "20260806_0010")
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO processed_events (event_id, status, processed_at) "
                    "VALUES ('evt-existing-outbox', 'dead', now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO reply_outbox "
                    "(id, event_id, message_id, reply_type, payload_json, status) "
                    "VALUES (:id, 'evt-existing-outbox', 'message-existing', "
                    "'text', CAST(:payload AS json), 'sent')"
                ),
                {"id": uuid.uuid4(), "payload": json.dumps({"text": "existing"})},
            )
        await asyncio.to_thread(upgrade, "head")

        async with scratch_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            replay_columns = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'processed_events' AND column_name IN "
                        "('manual_replay_count', 'replay_safety_version', "
                        "'business_committed_at')"
                    )
                )
            }
            audit_exists = await connection.scalar(
                text("SELECT to_regclass('public.event_replay_audits')")
            )
            committed_at = await connection.scalar(
                text(
                    "SELECT business_committed_at FROM processed_events "
                    "WHERE event_id = 'evt-existing-outbox'"
                )
            )
            assert revision == "20260806_0011"
            assert replay_columns == {
                "manual_replay_count",
                "replay_safety_version",
                "business_committed_at",
            }
            assert audit_exists == "event_replay_audits"
            assert committed_at is not None

        await asyncio.to_thread(downgrade, "20260806_0010")
        async with scratch_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            replay_columns = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'processed_events' AND column_name IN "
                        "('manual_replay_count', 'replay_safety_version', "
                        "'business_committed_at')"
                    )
                )
            }
            audit_exists = await connection.scalar(
                text("SELECT to_regclass('public.event_replay_audits')")
            )
            assert revision == "20260806_0010"
            assert replay_columns == set()
            assert audit_exists is None
    finally:
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
