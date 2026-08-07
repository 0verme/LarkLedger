from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.models import Base, ReplyOutbox
from lark_ledger.outbox import ReplyStatus, ReplyType, build_text_payload
from lark_ledger.services.outbox import ReplyOutboxStore

NOW = datetime(2026, 8, 7, 4, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=1)


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _insert(
    factory: async_sessionmaker[Any],
    *,
    event_id: str,
    status: str = ReplyStatus.SENDING.value,
    updated_at: datetime = CUTOFF - timedelta(minutes=1),
    lease_expires_at: datetime | None = NOW - timedelta(minutes=1),
    remote_message_id: str | None = None,
) -> ReplyOutbox:
    async with factory() as session:
        row = ReplyOutbox(
            event_id=event_id,
            message_id=f"om_{event_id}",
            reply_type=ReplyType.TEXT.value,
            sequence=0,
            payload_json=build_text_payload("sensitive reply text"),
            status=status,
            attempt_count=4,
            lease_owner="legacy-owner",
            lease_expires_at=lease_expires_at,
            remote_message_id=remote_message_id,
            created_at=updated_at - timedelta(minutes=5),
            updated_at=updated_at,
        )
        session.add(row)
        await session.commit()
        return row


async def test_reconcile_dry_run_filters_candidates_and_exposes_safe_metadata() -> None:
    engine, factory = await _sqlite_factory()
    candidate = await _insert(factory, event_id="candidate")
    await _insert(factory, event_id="unexpired", lease_expires_at=NOW + timedelta(minutes=1))
    await _insert(factory, event_id="after-cutoff", updated_at=CUTOFF + timedelta(seconds=1))
    await _insert(factory, event_id="already-sent", status=ReplyStatus.SENT.value)
    await _insert(factory, event_id="already-dead", status=ReplyStatus.DEAD.value)
    await _insert(factory, event_id="pending", status=ReplyStatus.PENDING.value)
    await _insert(factory, event_id="remote-known", remote_message_id="om_remote")

    results = await ReplyOutboxStore(factory).reconcile_legacy_owner_mismatch(
        before=CUTOFF, now=NOW
    )

    assert [item.id for item in results] == [candidate.id]
    safe_output = results[0].to_safe_dict()
    assert set(safe_output) == {
        "outbox_id",
        "event_id",
        "attempt_count",
        "created_at",
        "updated_at",
        "lease_expires_at",
    }
    assert "sensitive reply text" not in str(safe_output)
    async with factory() as session:
        reloaded = await session.get(ReplyOutbox, candidate.id)
        assert reloaded is not None
        assert reloaded.status == ReplyStatus.SENDING.value
        assert reloaded.lease_owner == "legacy-owner"
    await engine.dispose()


async def test_reconcile_execute_marks_dead_and_is_idempotent() -> None:
    engine, factory = await _sqlite_factory()
    candidate = await _insert(factory, event_id="candidate")
    store = ReplyOutboxStore(factory)

    first = await store.reconcile_legacy_owner_mismatch(
        before=CUTOFF, now=NOW, execute=True
    )
    second = await store.reconcile_legacy_owner_mismatch(
        before=CUTOFF, now=NOW + timedelta(minutes=1), execute=True
    )

    assert [item.id for item in first] == [candidate.id]
    assert second == []
    async with factory() as session:
        reloaded = await session.get(ReplyOutbox, candidate.id)
        assert reloaded is not None
        assert reloaded.status == ReplyStatus.DEAD.value
        assert reloaded.sent_at is None
        assert reloaded.remote_message_id is None
        assert reloaded.next_attempt_at is None
        assert reloaded.lease_owner is None
        assert reloaded.lease_expires_at is None
        assert reloaded.last_error_code == "LegacyOwnerMismatch"
        assert "suppressed to prevent duplicate reply" in str(reloaded.result_summary)
    await engine.dispose()


async def test_reconcile_rejects_future_cutoff() -> None:
    engine, factory = await _sqlite_factory()
    with pytest.raises(ValueError, match="cannot be in the future"):
        await ReplyOutboxStore(factory).reconcile_legacy_owner_mismatch(
            before=NOW + timedelta(seconds=1), now=NOW
        )
    await engine.dispose()
