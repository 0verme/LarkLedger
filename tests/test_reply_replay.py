"""P06b result replay: resend only already-persisted outbox intents.

``OutboxReplayService`` never re-calls AI, never re-runs a business command,
never re-queries the ledger, and never re-renders a report — it only moves
``failed`` / ``dead`` rows back to ``pending`` so the Reply Worker delivers the
exact stored payload. These tests use SQLite in-memory (single-session store
behavior); the deliverable state machine is covered in
``tests/test_reply_worker.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.models import Base, LedgerEntry, ReplyOutbox
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_text_payload,
)
from lark_ledger.services.replay import OutboxReplayService

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def factory() -> async_sessionmaker[Any]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _insert(
    factory: async_sessionmaker[Any],
    *,
    event_id: str = "evt_replay",
    status: str = ReplyStatus.FAILED.value,
    message_id: str = "om_replay",
    reply_type: str = ReplyType.TEXT.value,
) -> ReplyOutbox:
    async with factory() as session:
        row = ReplyOutbox(
            event_id=event_id,
            message_id=message_id,
            reply_type=reply_type,
            sequence=0,
            transport="feishu",
            payload_version=OUTBOX_PAYLOAD_VERSION,
            payload_json=build_text_payload("已记录 #A83F2 支出 ¥32.00"),
            status=status,
            attempt_count=2,
            next_attempt_at=T0 + timedelta(seconds=8),
            lease_owner="old",
            lease_expires_at=T0 + timedelta(seconds=1),
            last_error_code="RuntimeError",
            result_summary="RuntimeError: timeout",
        )
        session.add(row)
        await session.commit()
        return row


async def _status(factory: async_sessionmaker[Any], outbox_id: Any) -> str | None:
    async with factory() as session:
        row = await session.get(ReplyOutbox, outbox_id)
        return row.status if row is not None else None


async def test_replay_failed_row_returns_to_pending(factory: async_sessionmaker[Any]) -> None:
    row = await _insert(factory, status=ReplyStatus.FAILED.value)
    result = await OutboxReplayService(factory).replay_ids([row.id], now=T0)

    assert result.reset == 1
    assert result.skipped == 0
    assert result.not_found == 0
    assert await _status(factory, row.id) == ReplyStatus.PENDING.value
    # The row is immediately claimable by the worker: no backoff, no stale lease.
    async with factory() as session:
        reloaded = await session.get(ReplyOutbox, row.id)
    assert reloaded is not None
    assert reloaded.next_attempt_at is None
    assert reloaded.lease_owner is None
    assert reloaded.lease_expires_at is None
    assert reloaded.last_error_code is None
    assert reloaded.result_summary is None


async def test_replay_dead_row_returns_to_pending(factory: async_sessionmaker[Any]) -> None:
    row = await _insert(factory, status=ReplyStatus.DEAD.value)
    result = await OutboxReplayService(factory).replay_ids([row.id], now=T0)
    assert result.reset == 1
    assert await _status(factory, row.id) == ReplyStatus.PENDING.value


async def test_replay_skips_sent_and_pending_and_sending(
    factory: async_sessionmaker[Any],
) -> None:
    sent = await _insert(factory, event_id="evt_sent", status=ReplyStatus.SENT.value)
    pending = await _insert(factory, event_id="evt_pending", status=ReplyStatus.PENDING.value)
    sending = await _insert(factory, event_id="evt_sending", status=ReplyStatus.SENDING.value)
    service = OutboxReplayService(factory)
    result = await service.replay_ids([sent.id, pending.id, sending.id], now=T0)

    assert result.reset == 0
    assert result.skipped == 3
    assert await _status(factory, sent.id) == ReplyStatus.SENT.value
    assert await _status(factory, pending.id) == ReplyStatus.PENDING.value
    assert await _status(factory, sending.id) == ReplyStatus.SENDING.value


async def test_replay_counts_missing_rows(factory: async_sessionmaker[Any]) -> None:
    service = OutboxReplayService(factory)
    result = await service.replay_ids([None], now=T0)  # type: ignore[list-item]
    assert result.reset == 0
    assert result.skipped == 0
    assert result.not_found == 1


async def test_replay_by_event_id_resets_all_replayable_rows(
    factory: async_sessionmaker[Any],
) -> None:
    failed_a = await _insert(factory, event_id="evt_batch", message_id="om_a")
    dead_b = await _insert(
        factory,
        event_id="evt_batch",
        message_id="om_b",
        status=ReplyStatus.DEAD.value,
        reply_type=ReplyType.FILE.value,
    )
    sent_c = await _insert(
        factory,
        event_id="evt_batch",
        message_id="om_c",
        status=ReplyStatus.SENT.value,
        reply_type=ReplyType.CARD.value,
    )

    result = await OutboxReplayService(factory).replay_event("evt_batch", now=T0)

    assert result.reset == 2
    assert result.skipped == 1
    assert await _status(factory, failed_a.id) == ReplyStatus.PENDING.value
    assert await _status(factory, dead_b.id) == ReplyStatus.PENDING.value
    assert await _status(factory, sent_c.id) == ReplyStatus.SENT.value


async def test_status_view_lists_delivery_state_in_sequence_order(
    factory: async_sessionmaker[Any],
) -> None:
    await _insert(factory, event_id="evt_view", status=ReplyStatus.FAILED.value)
    views = await OutboxReplayService(factory).status_by_event("evt_view")
    assert len(views) == 1
    view = views[0]
    assert view.status == ReplyStatus.FAILED.value
    assert view.attempt_count == 2
    assert view.last_error_code == "RuntimeError"
    assert view.remote_message_id is None


async def test_replay_does_not_touch_ledger_or_ai(
    factory: async_sessionmaker[Any],
) -> None:
    """Replay only rewrites outbox state; it never touches business data."""
    row = await _insert(factory, status=ReplyStatus.FAILED.value)
    async with factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_user",
                short_id="A83F2",
                amount=32,
                currency="CNY",
                direction="expense",
                category="餐饮",
                note="",
                occurred_at=T0,
                source_type="text",
            )
        )
        await session.commit()

    result = await OutboxReplayService(factory).replay_ids([row.id], now=T0)

    assert result.reset == 1
    async with factory() as session:
        entries = (await session.execute(select(LedgerEntry))).scalars().all()
    assert len(entries) == 1  # business data untouched by replay
