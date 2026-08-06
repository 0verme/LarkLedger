import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.event_payload import EventProcessStatus
from lark_ledger.models import (
    Base,
    Direction,
    EventReplayAudit,
    LedgerEntry,
    LedgerEntryRevision,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.outbox import ReplyStatus
from lark_ledger.services.cleanup import (
    CLEANUP_TASK_NAME,
    CleanupResult,
    CleanupService,
    CleanupStore,
    CleanupWorker,
    RetentionPolicy,
)

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


async def database() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def event_row(
    event_id: str,
    status: EventProcessStatus,
    when: datetime,
    *,
    lease_expires_at: datetime | None = None,
) -> ProcessedEvent:
    return ProcessedEvent(
        event_id=event_id,
        status=status.value,
        processed_at=when,
        updated_at=when,
        lease_expires_at=lease_expires_at,
    )


def outbox_row(
    event_id: str,
    status: ReplyStatus,
    when: datetime,
    *,
    reply_type: str = "text",
    sent_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> ReplyOutbox:
    return ReplyOutbox(
        event_id=event_id,
        message_id=f"message-{event_id}",
        reply_type=reply_type,
        payload_json={"text": "private financial content"},
        status=status.value,
        sent_at=sent_at,
        created_at=when,
        updated_at=when,
        lease_expires_at=lease_expires_at,
    )


def service(factory: async_sessionmaker[AsyncSession], *, batch_size: int = 500) -> CleanupService:
    return CleanupService(CleanupStore(factory), RetentionPolicy(), batch_size=batch_size)


async def statuses(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[set[str], set[uuid.UUID]]:
    async with factory() as session:
        events = set((await session.scalars(select(ProcessedEvent.event_id))).all())
        outboxes = set((await session.scalars(select(ReplyOutbox.id))).all())
    return events, outboxes


async def test_cleanup_deletes_only_expired_terminal_rows_and_skips_active_leases(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, factory = await database()
    old_success = NOW - timedelta(days=31)
    old_dead = NOW - timedelta(days=91)
    fresh = NOW - timedelta(days=1)
    active_lease = NOW + timedelta(minutes=5)
    async with factory() as session:
        rows = [
            event_row("old-success", EventProcessStatus.SUCCEEDED, old_success),
            event_row("old-legacy", EventProcessStatus.LEGACY_SUCCEEDED, old_success),
            event_row("old-dead", EventProcessStatus.DEAD, old_dead),
            event_row("fresh-success", EventProcessStatus.SUCCEEDED, fresh),
            event_row("received", EventProcessStatus.RECEIVED, old_dead),
            event_row("processing", EventProcessStatus.PROCESSING, old_dead),
            event_row("failed", EventProcessStatus.FAILED, old_dead),
            event_row(
                "leased-terminal",
                EventProcessStatus.SUCCEEDED,
                old_success,
                lease_expires_at=active_lease,
            ),
        ]
        session.add_all(rows)
        for name, reply_status, when in (
            ("sent-old", ReplyStatus.SENT, old_success),
            ("dead-old", ReplyStatus.DEAD, old_dead),
            ("pending-old", ReplyStatus.PENDING, old_dead),
            ("sending-old", ReplyStatus.SENDING, old_dead),
            ("failed-old", ReplyStatus.FAILED, old_dead),
            ("sent-fresh", ReplyStatus.SENT, fresh),
        ):
            session.add(event_row(name, EventProcessStatus.FAILED, old_dead))
            session.add(
                outbox_row(
                    name,
                    reply_status,
                    when,
                    sent_at=when if reply_status is ReplyStatus.SENT else None,
                )
            )
        session.add(event_row("sent-leased", EventProcessStatus.FAILED, old_dead))
        session.add(
            outbox_row(
                "sent-leased",
                ReplyStatus.SENT,
                old_success,
                sent_at=old_success,
                lease_expires_at=active_lease,
            )
        )
        await session.commit()

    caplog.set_level("INFO", logger="lark_ledger.services.cleanup")
    result = await service(factory).run_once(now=NOW)
    remaining_events, remaining_outboxes = await statuses(factory)

    assert result == CleanupResult(
        outbox_sent=1,
        outbox_dead=1,
        event_succeeded=1,
        event_legacy_succeeded=1,
        event_dead=1,
    )
    assert {"old-success", "old-legacy", "old-dead"}.isdisjoint(remaining_events)
    assert {
        "fresh-success",
        "received",
        "processing",
        "failed",
        "leased-terminal",
    }.issubset(remaining_events)
    assert len(remaining_outboxes) == 5
    assert "kind=outbox_sent" in caplog.text
    assert "deleted=1" in caplog.text
    assert "private financial content" not in caplog.text
    assert "message-sent-old" not in caplog.text
    await engine.dispose()


async def test_sent_uses_sent_at_and_dead_uses_updated_at() -> None:
    engine, factory = await database()
    old = NOW - timedelta(days=100)
    fresh = NOW - timedelta(days=1)
    async with factory() as session:
        for event_id in ("sent-recent", "sent-old", "dead-recent", "dead-old"):
            session.add(event_row(event_id, EventProcessStatus.FAILED, old))
        recent_sent = outbox_row("sent-recent", ReplyStatus.SENT, old, sent_at=fresh)
        old_sent = outbox_row("sent-old", ReplyStatus.SENT, fresh, sent_at=old)
        recent_dead = outbox_row("dead-recent", ReplyStatus.DEAD, fresh)
        old_dead = outbox_row("dead-old", ReplyStatus.DEAD, old)
        session.add_all([recent_sent, old_sent, recent_dead, old_dead])
        await session.commit()
        keep_ids = {recent_sent.id, recent_dead.id}

    result = await service(factory).run_once(now=NOW)
    _, remaining = await statuses(factory)

    assert result.outbox_sent == 1
    assert result.outbox_dead == 1
    assert remaining == keep_ids
    await engine.dispose()


async def test_event_waits_until_all_associated_outbox_audit_has_expired() -> None:
    engine, factory = await database()
    event_time = NOW - timedelta(days=100)
    outbox_time = NOW - timedelta(days=60)
    async with factory() as session:
        session.add(event_row("audit-retained", EventProcessStatus.DEAD, event_time))
        session.add(outbox_row("audit-retained", ReplyStatus.DEAD, outbox_time))
        await session.commit()

    first = await service(factory).run_once(now=NOW)
    first_events, first_outboxes = await statuses(factory)
    assert first.total == 0
    assert first_events == {"audit-retained"}
    assert len(first_outboxes) == 1

    second = await service(factory).run_once(now=NOW + timedelta(days=31))
    second_events, second_outboxes = await statuses(factory)
    assert second.outbox_dead == 1
    assert second.event_dead == 1
    assert second_events == set()
    assert second_outboxes == set()
    await engine.dispose()


async def test_batch_size_bounds_each_delete_and_repeated_runs_finish() -> None:
    engine, factory = await database()
    old = NOW - timedelta(days=31)
    async with factory() as session:
        for index in range(3):
            event_id = f"batch-{index}"
            session.add(event_row(event_id, EventProcessStatus.FAILED, old))
            session.add(outbox_row(event_id, ReplyStatus.SENT, old, sent_at=old))
        await session.commit()

    cleanup = service(factory, batch_size=2)
    first = await cleanup.run_once(now=NOW)
    _, after_first = await statuses(factory)
    second = await cleanup.run_once(now=NOW)
    _, after_second = await statuses(factory)

    assert first.outbox_sent == 2
    assert len(after_first) == 1
    assert second.outbox_sent == 1
    assert after_second == set()
    await engine.dispose()


async def test_later_batch_failure_keeps_earlier_committed_delete() -> None:
    engine, factory = await database()
    old = NOW - timedelta(days=100)
    async with factory() as session:
        session.add(event_row("sent", EventProcessStatus.FAILED, old))
        sent = outbox_row("sent", ReplyStatus.SENT, old, sent_at=old)
        session.add(sent)
        session.add(event_row("event", EventProcessStatus.SUCCEEDED, old))
        await session.commit()
        sent_id = sent.id

    class FailAfterSentStore(CleanupStore):
        async def delete_outbox_batch(self, **kwargs: Any) -> int:
            if kwargs["status"] is ReplyStatus.DEAD:
                raise RuntimeError("injected batch failure")
            return await super().delete_outbox_batch(**kwargs)

    cleanup = CleanupService(FailAfterSentStore(factory), RetentionPolicy())
    with pytest.raises(RuntimeError, match="injected batch failure"):
        await cleanup.run_once(now=NOW)

    async with factory() as session:
        assert await session.get(ReplyOutbox, sent_id) is None
        assert await session.get(ProcessedEvent, "event") is not None
    await engine.dispose()


async def test_cleanup_never_deletes_ledger_entries_or_revisions() -> None:
    engine, factory = await database()
    old = NOW - timedelta(days=100)
    entry_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            LedgerEntry(
                id=entry_id,
                user_open_id="user-private",
                short_id="SAFE1",
                amount=Decimal("12.34"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="test",
                note="private note",
                occurred_at=old,
            )
        )
        session.add(
            LedgerEntryRevision(
                entry_id=entry_id,
                user_open_id="user-private",
                short_id="SAFE1",
                change_type="update",
                before_json={"note": "private"},
                after_json={"note": "still private"},
            )
        )
        session.add(event_row("cleanup-only", EventProcessStatus.SUCCEEDED, old))
        session.add(
            EventReplayAudit(
                event_id="cleanup-only",
                operator="operator",
                reason="audit must outlive terminal event cleanup",
                previous_status=EventProcessStatus.DEAD.value,
                previous_attempt_count=3,
                replay_number=1,
                action="replay_event",
                outcome="requeued",
                resulting_status=EventProcessStatus.RECEIVED.value,
                replayed_at=old,
            )
        )
        await session.commit()

    await service(factory).run_once(now=NOW)

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(LedgerEntryRevision)) == 1
        assert await session.get(ProcessedEvent, "cleanup-only") is None
        assert await session.scalar(select(func.count()).select_from(EventReplayAudit)) == 1
    await engine.dispose()


async def test_cleanup_worker_starts_retries_round_failure_and_stops_cleanly() -> None:
    calls = 0
    slept = asyncio.Event()

    class FlakyService:
        async def run_once(self, *, now: datetime | None = None) -> CleanupResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("one round failed")
            return CleanupResult()

    async def sleeper(_delay: float) -> None:
        slept.set()
        await asyncio.Event().wait()

    worker = CleanupWorker(FlakyService(), sleeper=sleeper)  # type: ignore[arg-type]
    worker.start()
    await asyncio.wait_for(slept.wait(), timeout=1)
    assert worker.running is True
    await worker.stop()

    assert worker.running is False
    assert worker.health_snapshot()["task_exception"] is False
    assert [task for task in asyncio.all_tasks() if task.get_name() == CLEANUP_TASK_NAME] == []


def test_cleanup_config_defaults_and_zero_or_negative_values_are_rejected() -> None:
    settings = Settings(_env_file=None)
    assert settings.cleanup_enabled is True
    assert settings.cleanup_interval_seconds == 3600.0
    assert settings.cleanup_batch_size == 500
    assert settings.event_succeeded_retention_days == 30
    assert settings.event_dead_retention_days == 90
    assert settings.outbox_sent_retention_days == 30
    assert settings.outbox_dead_retention_days == 90

    for kwargs in (
        {"cleanup_interval_seconds": 0},
        {"cleanup_batch_size": 0},
        {"event_succeeded_retention_days": 0},
        {"event_dead_retention_days": -1},
        {"outbox_sent_retention_days": 0},
        {"outbox_dead_retention_days": -1},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **kwargs)


def test_policy_and_service_reject_unsafe_values() -> None:
    with pytest.raises(ValueError, match="at least one day"):
        RetentionPolicy(event_succeeded_days=0)
    with pytest.raises(ValueError, match="positive"):
        CleanupService(object(), RetentionPolicy(), batch_size=0)  # type: ignore[arg-type]
