"""P06b PostgreSQL integration: reply worker claim / lease / ordering.

Exercises the real ``FOR UPDATE SKIP LOCKED`` claim, lease takeover, per-event
ordering under concurrency, and the ``20260806_0009`` migration roundtrip on
real storage. The schema is created by ``alembic upgrade head`` (CI runs it
before this suite); the ``postgres_engine`` fixture truncates all tables
between tests.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from lark_ledger.event_payload import EventProcessStatus
from lark_ledger.models import ProcessedEvent, ReplyOutbox
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_file_payload,
    build_text_payload,
)
from lark_ledger.services.outbox import ReplyOutboxStore
from lark_ledger.services.reply_worker import ReplyDeliverer

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _file_bytes() -> bytes:
    return b"short_id,amount\n#A83F2,32.00\n"


class RecordingFeishu:
    def __init__(self, *, reply_error: BaseException | None = None) -> None:
        self.reply_error = reply_error
        self.text_calls: list[str] = []

    async def reply_text(self, message_id: str, text: str, *, uuid: str | None = None) -> None:
        if self.reply_error is not None:
            raise self.reply_error
        self.text_calls.append(text)

    async def upload_file(self, content: bytes, filename: str) -> str:
        return "file_key_1"

    async def reply_file(
        self, message_id: str, file_key: str, *, uuid: str | None = None
    ) -> None:
        pass

    async def upload_image(self, png: bytes) -> str:
        return "image_key_1"

    async def reply_card(
        self, message_id: str, card: dict[str, Any], *, uuid: str | None = None
    ) -> None:
        pass


async def _insert(
    factory: async_sessionmaker[Any],
    *,
    event_id: str,
    message_id: str,
    reply_type: str = ReplyType.TEXT.value,
    sequence: int = 0,
    payload: dict[str, Any] | None = None,
    blob: bytes | None = None,
    status: str = ReplyStatus.PENDING.value,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> None:
    if payload is None:
        payload = build_text_payload("hello")
    async with factory() as session:
        # PostgreSQL enforces the reply_outbox -> processed_events FK, so every
        # outbox row needs a parent event row (SQLite does not enforce it by
        # default, which is why the unit tests can skip this).
        if event_id is not None and await session.get(ProcessedEvent, event_id) is None:
            session.add(
                ProcessedEvent(
                    event_id=event_id,
                    status=EventProcessStatus.SUCCEEDED.value,
                )
            )
        session.add(
            ReplyOutbox(
                event_id=event_id,
                message_id=message_id,
                reply_type=reply_type,
                sequence=sequence,
                transport="feishu",
                payload_version=OUTBOX_PAYLOAD_VERSION,
                payload_json=payload,
                payload_blob=blob,
                status=status,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
        )
        await session.commit()


async def _row(factory: async_sessionmaker[Any], outbox_id: Any) -> ReplyOutbox:
    async with factory() as session:
        row = await session.get(ReplyOutbox, outbox_id)
        assert row is not None
        return row


def _deliverer(store: ReplyOutboxStore, owner: str) -> ReplyDeliverer:
    return ReplyDeliverer(store, RecordingFeishu(), owner_id=owner, jitter=None)


async def test_claim_sets_timezone_aware_lease_on_postgres(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, event_id="evt_ts", message_id="om_ts")
    claimed = await ReplyOutboxStore(postgres_session_factory).claim_batch(
        "w1", T0, batch_size=10, lease_seconds=300.0
    )
    assert len(claimed) == 1
    assert claimed[0].attempt_count == 1
    row = await _row(postgres_session_factory, claimed[0].id)
    assert row.status == ReplyStatus.SENDING.value
    assert row.lease_owner == "w1"
    assert row.lease_expires_at == T0 + timedelta(seconds=300)
    # Production storage must return timezone-aware lease timestamps.
    assert row.lease_expires_at is not None and row.lease_expires_at.tzinfo is not None


async def test_concurrent_workers_never_claim_the_same_outbox(
    postgres_engine: AsyncEngine,
) -> None:
    factory_a = async_sessionmaker(postgres_engine, expire_on_commit=False)
    for index in range(4):
        await _insert(factory_a, event_id=f"evt_c{index}", message_id=f"om_c{index}")

    async def claim(owner: str) -> list[uuid.UUID]:
        return [
            item.id
            for item in await ReplyOutboxStore(
                async_sessionmaker(postgres_engine, expire_on_commit=False)
            ).claim_batch(owner, T0, batch_size=10, lease_seconds=300.0)
        ]

    results = await asyncio.gather(claim("w-a"), claim("w-b"))
    all_claimed = results[0] + results[1]
    assert len(all_claimed) == 4
    assert len(set(all_claimed)) == 4  # SKIP LOCKED: no outbox claimed twice

    for outbox_id in all_claimed:
        row = await _row(factory_a, outbox_id)
        assert row.status == ReplyStatus.SENDING.value
        assert row.attempt_count == 1


async def test_claim_commits_before_delivery_starts(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, event_id="evt_tx", message_id="om_tx")
    observed: list[str] = []

    class InspectingFeishu(RecordingFeishu):
        async def reply_text(
            self, message_id: str, text: str, *, uuid: str | None = None
        ) -> None:
            # The claim transaction must already be committed and visible from a
            # separate session before any Feishu call happens.
            async with postgres_session_factory() as session:
                row = await session.scalar(select(ReplyOutbox))
                assert row is not None
                assert row.status == ReplyStatus.SENDING.value
                assert row.lease_owner == "w1"
                assert row.attempt_count == 1
            observed.append("delivered")

    store = ReplyOutboxStore(postgres_session_factory)
    deliverer = ReplyDeliverer(store, InspectingFeishu(), owner_id="w1", jitter=None)
    claimed = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert len(claimed) == 1
    await deliverer.process_item(claimed[0], T0)
    assert observed == ["delivered"]


async def test_only_lease_owner_can_complete_on_postgres(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, event_id="evt_own", message_id="om_own")
    store = ReplyOutboxStore(postgres_session_factory)
    claimed = await store.claim_batch("w-a", T0, batch_size=10, lease_seconds=300.0)
    assert len(claimed) == 1
    item = claimed[0]

    # A non-owner cannot complete the row.
    assert await store.mark_sent(item.id, "w-b", now=T0) is False
    row = await _row(postgres_session_factory, item.id)
    assert row.status == ReplyStatus.SENDING.value
    assert row.lease_owner == "w-a"

    assert await store.mark_sent(item.id, "w-a", now=T0) is True
    row = await _row(postgres_session_factory, item.id)
    assert row.status == ReplyStatus.SENT.value
    assert row.lease_owner is None
    assert row.lease_expires_at is None


async def test_expired_lease_reclaim_and_stale_worker_cannot_overwrite(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, event_id="evt_reclaim", message_id="om_reclaim")
    store = ReplyOutboxStore(postgres_session_factory)
    claimed = await store.claim_batch("old", T0, batch_size=10, lease_seconds=300.0)
    assert len(claimed) == 1
    item = claimed[0]

    later = T0 + timedelta(seconds=301)
    reclaimed = await store.claim_batch("new", later, batch_size=10, lease_seconds=300.0)
    assert [r.id for r in reclaimed] == [item.id]
    assert reclaimed[0].attempt_count == 2

    # The stale worker's lease is gone; it must not overwrite the new owner.
    assert await store.mark_sent(item.id, "old", now=later) is False
    assert (
        await store.record_failure(
            item.id, "old", status=ReplyStatus.DEAD.value, next_attempt_at=None,
            error_code="X", summary="stale", now=later,
        )
        is False
    )
    assert await store.mark_sent(item.id, "new", now=later) is True


async def test_ordering_holds_under_concurrent_workers(
    postgres_engine: AsyncEngine,
) -> None:
    """File (seq 0) before confirmation text (seq 1): two workers claiming
    simultaneously must never let the text overtake the file."""
    event_id = "evt_concurrent_order"
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    await _insert(
        factory,
        event_id=event_id,
        message_id="om_order",
        reply_type=ReplyType.FILE.value,
        sequence=0,
        payload=build_file_payload(
            filename="larkledger-export-v1.csv",
            content_type="text/csv",
            content=_file_bytes(),
        ),
        blob=_file_bytes(),
    )
    await _insert(
        factory,
        event_id=event_id,
        message_id="om_order",
        reply_type=ReplyType.TEXT.value,
        sequence=1,
        payload=build_text_payload("已导出 1 笔"),
    )

    async def claim(owner: str) -> list[str]:
        store = ReplyOutboxStore(async_sessionmaker(postgres_engine, expire_on_commit=False))
        claimed = await store.claim_batch(owner, T0, batch_size=10, lease_seconds=300.0)
        return [item.reply_type for item in claimed]

    results = await asyncio.gather(claim("w-a"), claim("w-b"))
    first_wave = results[0] + results[1]
    # Only seq 0 (file) may be claimed; seq 1 (text) must wait for it.
    assert sorted(first_wave) == ["file"]
    assert len(first_wave) == 1


async def test_failed_retry_is_timezone_aware(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, event_id="evt_retry", message_id="om_retry")
    store = ReplyOutboxStore(postgres_session_factory)
    item = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert len(item) == 1
    feishu = RecordingFeishu(reply_error=RuntimeError("timeout"))
    deliverer = ReplyDeliverer(store, feishu, owner_id="w1", jitter=None, retry_base_seconds=2.0)
    outcome = await deliverer.process_item(item[0], T0)
    assert outcome == ReplyStatus.FAILED.value
    row = await _row(postgres_session_factory, item[0].id)
    assert row.status == ReplyStatus.FAILED.value
    assert row.next_attempt_at == T0 + timedelta(seconds=2)
    assert row.next_attempt_at is not None and row.next_attempt_at.tzinfo is not None
    assert row.lease_owner is None


@pytest.mark.postgres
async def test_reply_delivery_migration_roundtrip_0009(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade 0008→head adds reply delivery columns + index; downgrade drops them."""
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_reply_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(_run_migrations, "20260806_0008")
        await asyncio.to_thread(_run_migrations, "20260806_0009")

        async with scratch_engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20260806_0009"
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'reply_outbox' ORDER BY column_name"
                )
            )
            names = {row[0] for row in columns}
            for expected in ("remote_message_id", "remote_file_key", "remote_image_key"):
                assert expected in names, f"reply_outbox missing column {expected}"
            index = await conn.scalar(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'reply_outbox' AND indexname = 'ix_outbox_event_sequence'"
                )
            )
            assert index == "ix_outbox_event_sequence"

        await asyncio.to_thread(_run_downgrade, "20260806_0008")
        async with scratch_engine.connect() as conn:
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'reply_outbox' ORDER BY column_name"
                )
            )
            names = {row[0] for row in columns}
            assert "remote_message_id" not in names
            assert "remote_file_key" not in names
            assert "remote_image_key" not in names
            index = await conn.scalar(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'reply_outbox' AND indexname = 'ix_outbox_event_sequence'"
                )
            )
            assert index is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
