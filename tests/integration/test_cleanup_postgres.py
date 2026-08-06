"""P06d PostgreSQL integration for lock-safe cleanup and migration indexes."""

from __future__ import annotations

import asyncio
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
from lark_ledger.event_payload import EventProcessStatus
from lark_ledger.models import ProcessedEvent, ReplyOutbox
from lark_ledger.outbox import ReplyStatus
from lark_ledger.services.cleanup import CleanupService, CleanupStore, RetentionPolicy

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def parent(event_id: str) -> ProcessedEvent:
    old = NOW - timedelta(days=100)
    return ProcessedEvent(
        event_id=event_id,
        status=EventProcessStatus.FAILED.value,
        processed_at=old,
        updated_at=old,
    )


def sent_reply(event_id: str, sent_at: datetime) -> ReplyOutbox:
    return ReplyOutbox(
        event_id=event_id,
        message_id=f"message-{event_id}",
        reply_type="text",
        payload_json={"text": "integration private content"},
        status=ReplyStatus.SENT.value,
        sent_at=sent_at,
        updated_at=sent_at,
    )


async def test_concurrent_cleanup_instances_skip_locked_rows_without_errors(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old = NOW - timedelta(days=31)
    async with postgres_session_factory() as session:
        for index in range(20):
            event_id = f"cleanup-concurrent-{index}"
            session.add(parent(event_id))
            session.add(sent_reply(event_id, old))
        await session.commit()

    first, second = await asyncio.gather(
        CleanupService(
            CleanupStore(postgres_session_factory), RetentionPolicy(), batch_size=10
        ).run_once(now=NOW),
        CleanupService(
            CleanupStore(postgres_session_factory), RetentionPolicy(), batch_size=10
        ).run_once(now=NOW),
    )

    async with postgres_session_factory() as session:
        remaining = await session.scalar(select(func.count()).select_from(ReplyOutbox))
    assert first.outbox_sent + second.outbox_sent == 20
    assert remaining == 0


async def test_cleanup_retention_boundary_is_inclusive_and_timezone_aware(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cutoff = NOW - timedelta(days=30)
    async with postgres_session_factory() as session:
        session.add_all([parent("at-cutoff"), parent("after-cutoff")])
        exact = sent_reply("at-cutoff", cutoff)
        recent = sent_reply("after-cutoff", cutoff + timedelta(microseconds=1))
        session.add_all([exact, recent])
        await session.commit()
        exact_id = exact.id
        recent_id = recent.id

    result = await CleanupService(
        CleanupStore(postgres_session_factory), RetentionPolicy()
    ).run_once(now=NOW)

    async with postgres_session_factory() as session:
        assert await session.get(ReplyOutbox, exact_id) is None
        assert await session.get(ReplyOutbox, recent_id) is not None
    assert result.outbox_sent == 1


async def test_cleanup_index_migration_upgrade_downgrade_roundtrip(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = make_url(postgres_url)
    base_dsn = url.set(database="postgres").render_as_string(hide_password=False)
    scratch = f"lark_ledger_cleanup_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)

    def migrate(target: str) -> None:
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
        await asyncio.to_thread(migrate, "20260806_0009")
        await asyncio.to_thread(migrate, "20260806_0010")

        expected = {
            "ix_events_cleanup_processed",
            "ix_events_cleanup_updated",
            "ix_outbox_cleanup_sent",
            "ix_outbox_cleanup_updated",
        }
        async with scratch_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            rows = await connection.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename IN ('processed_events', 'reply_outbox')"
                )
            )
            assert revision == "20260806_0010"
            assert expected.issubset({row[0] for row in rows})

        await asyncio.to_thread(downgrade, "20260806_0009")
        async with scratch_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            rows = await connection.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename IN ('processed_events', 'reply_outbox')"
                )
            )
            assert revision == "20260806_0009"
            assert expected.isdisjoint({row[0] for row in rows})
    finally:
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
