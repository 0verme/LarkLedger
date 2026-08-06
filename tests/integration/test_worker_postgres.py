"""P05b PostgreSQL integration: worker claim / lease / retry / dead.

Exercises the real ``FOR UPDATE SKIP LOCKED`` claim, lease takeover, attempt
budget, exponential backoff, permanent-error dead-lettering, and business
idempotency under retry. The schema is created by ``alembic upgrade head``
(CI runs it before this suite); the ``postgres_engine`` fixture truncates all
tables between tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lark_ledger.event_payload import (
    EventPayloadError,
    EventProcessStatus,
    build_stored_payload,
    serialize_payload,
)
from lark_ledger.models import Direction, LedgerEntry, ProcessedEvent
from lark_ledger.services.events import EventService
from lark_ledger.services.worker import (
    ClaimedEvent,
    EventWorker,
    EventWorkerStore,
)

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


class RecordingProcessor:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.exc = exc

    async def process(self, event: dict[str, Any]) -> None:
        if self.exc is not None:
            raise self.exc
        self.events.append(event)


def _payload(event_id: str, message_id: str = "om_w") -> dict[str, Any]:
    return serialize_payload(
        build_stored_payload(
            event_id,
            {
                "sender": {"sender_id": {"open_id": "ou_worker"}},
                "message": {
                    "message_id": message_id,
                    "message_type": "text",
                    "content": '{"text":"hi"}',
                },
            },
            transport="webhook",
            received_at=T0,
        )
    )


async def _insert(
    factory: async_sessionmaker[Any],
    event_id: str,
    *,
    status: str = EventProcessStatus.RECEIVED.value,
    attempt_count: int = 0,
    payload: dict[str, Any] | None = None,
    next_attempt_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    received_at: datetime = T0,
) -> None:
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id=event_id,
                payload_json=payload,
                payload_version=payload.get("payload_version") if payload else None,
                status=status,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                received_at=received_at,
            )
        )
        await session.commit()


async def _row(
    factory: async_sessionmaker[Any], event_id: str
) -> ProcessedEvent:
    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None
        return row


def _worker(
    factory: async_sessionmaker[Any],
    processor: Any,
    *,
    owner_id: str = "w1",
    **kwargs: Any,
) -> EventWorker:
    return EventWorker(
        EventWorkerStore(factory),
        processor,
        owner_id=owner_id,
        jitter=None,
        **kwargs,
    )


async def test_claim_received_event_sets_lease_and_attempt(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_r", payload=_payload("evt_r"))
    store = EventWorkerStore(postgres_session_factory)
    claimed = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert claimed == [ClaimedEvent("evt_r", 1)]

    row = await _row(postgres_session_factory, "evt_r")
    assert row.status == EventProcessStatus.PROCESSING.value
    assert row.lease_owner == "w1"
    assert row.attempt_count == 1
    assert row.lease_expires_at == T0 + timedelta(seconds=300)
    # Production storage must return timezone-aware lease timestamps.
    assert row.lease_expires_at is not None and row.lease_expires_at.tzinfo is not None


async def test_claim_condition_matrix(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    future = T0 + timedelta(hours=1)
    past = T0 - timedelta(hours=1)
    await _insert(postgres_session_factory, "recv", payload=_payload("recv"))
    await _insert(
        postgres_session_factory,
        "failed_due",
        status=EventProcessStatus.FAILED.value,
        attempt_count=1,
        payload=_payload("failed_due"),
        next_attempt_at=past,
    )
    await _insert(
        postgres_session_factory,
        "failed_future",
        status=EventProcessStatus.FAILED.value,
        attempt_count=1,
        payload=_payload("failed_future"),
        next_attempt_at=future,
    )
    await _insert(
        postgres_session_factory,
        "proc_active",
        status=EventProcessStatus.PROCESSING.value,
        attempt_count=1,
        payload=_payload("proc_active"),
        lease_owner="other",
        lease_expires_at=future,
    )
    await _insert(
        postgres_session_factory,
        "proc_expired",
        status=EventProcessStatus.PROCESSING.value,
        attempt_count=1,
        payload=_payload("proc_expired"),
        lease_owner="old-worker",
        lease_expires_at=past,
    )
    await _insert(
        postgres_session_factory,
        "done",
        status=EventProcessStatus.SUCCEEDED.value,
        payload=_payload("done"),
    )
    await _insert(
        postgres_session_factory,
        "dead",
        status=EventProcessStatus.DEAD.value,
        payload=_payload("dead"),
    )
    await _insert(
        postgres_session_factory,
        "legacy",
        status=EventProcessStatus.LEGACY_SUCCEEDED.value,
        payload=None,
    )

    claimed = await EventWorkerStore(postgres_session_factory).claim_batch(
        "w1", T0, batch_size=10, lease_seconds=300.0
    )
    claimed_ids = {item.event_id for item in claimed}
    attempts = {item.event_id: item.attempt_count for item in claimed}

    assert "recv" in claimed_ids
    assert "failed_due" in claimed_ids
    assert "failed_future" not in claimed_ids
    assert "proc_active" not in claimed_ids
    assert "proc_expired" in claimed_ids
    assert "done" not in claimed_ids
    assert "dead" not in claimed_ids
    assert "legacy" not in claimed_ids
    assert attempts["proc_expired"] == 2  # lease reclaim is a second attempt
    assert attempts["recv"] == 1


async def test_concurrent_workers_never_claim_the_same_event(
    postgres_engine: AsyncEngine,
) -> None:
    factory_a = async_sessionmaker(postgres_engine, expire_on_commit=False)
    factory_b = async_sessionmaker(postgres_engine, expire_on_commit=False)
    for index in range(4):
        await _insert(factory_a, f"evt_c{index}", payload=_payload(f"evt_c{index}"))

    async def claim(store: EventWorkerStore, owner: str) -> list[ClaimedEvent]:
        return await store.claim_batch(owner, T0, batch_size=10, lease_seconds=300.0)

    results = await asyncio.gather(
        claim(EventWorkerStore(factory_a), "w-a"),
        claim(EventWorkerStore(factory_b), "w-b"),
    )
    all_claimed = [item.event_id for item in results[0] + results[1]]
    assert len(all_claimed) == 4
    assert len(set(all_claimed)) == 4  # SKIP LOCKED: no row claimed twice

    for event_id in all_claimed:
        row = await _row(factory_a, event_id)
        assert row.status == EventProcessStatus.PROCESSING.value
        assert row.attempt_count == 1


async def test_claim_commits_before_processing_starts(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_tx", payload=_payload("evt_tx"))
    observed: list[str] = []

    class InspectingProcessor:
        async def process(self, event: dict[str, Any]) -> None:
            # The claim transaction must already be committed and visible: the
            # row is processing + leased from a separate session.
            async with postgres_session_factory() as session:
                row = await session.get(ProcessedEvent, "evt_tx")
                assert row is not None
                assert row.status == EventProcessStatus.PROCESSING.value
                assert row.lease_owner == "w1"
                assert row.attempt_count == 1
            observed.append("processed")

    worker = _worker(postgres_session_factory, InspectingProcessor(), owner_id="w1")
    await worker.run_once(now=T0)
    assert observed == ["processed"]
    row = await _row(postgres_session_factory, "evt_tx")
    assert row.status == EventProcessStatus.SUCCEEDED.value


async def test_only_lease_owner_can_complete(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_own", payload=_payload("evt_own"))
    store = EventWorkerStore(postgres_session_factory)
    assert await store.claim_batch("w-a", T0, batch_size=10, lease_seconds=300.0)

    # A non-owner cannot complete the event.
    assert await store.complete("evt_own", "w-b", T0) is False
    row = await _row(postgres_session_factory, "evt_own")
    assert row.status == EventProcessStatus.PROCESSING.value
    assert row.lease_owner == "w-a"

    assert await store.complete("evt_own", "w-a", T0) is True
    row = await _row(postgres_session_factory, "evt_own")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.next_attempt_at is None
    assert row.last_error_code is None
    assert row.result_summary is None


async def test_expired_lease_reclaim_and_stale_worker_cannot_overwrite(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_reclaim", payload=_payload("evt_reclaim"))
    store = EventWorkerStore(postgres_session_factory)
    assert await store.claim_batch("old", T0, batch_size=10, lease_seconds=300.0)

    later = T0 + timedelta(seconds=301)
    reclaimed = await store.claim_batch("new", later, batch_size=10, lease_seconds=300.0)
    assert [item.event_id for item in reclaimed] == ["evt_reclaim"]
    assert reclaimed[0].attempt_count == 2

    # The stale worker's lease is gone; it must not overwrite the new owner.
    assert await store.complete("evt_reclaim", "old", later) is False
    assert await store.record_failure(
        "evt_reclaim",
        "old",
        status=EventProcessStatus.DEAD.value,
        next_attempt_at=None,
        error_code="X",
        summary="stale",
        now=later,
    ) is False

    assert await store.complete("evt_reclaim", "new", later) is True
    row = await _row(postgres_session_factory, "evt_reclaim")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.lease_owner is None


async def test_retryable_failure_schedules_exponential_backoff(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_retry", payload=_payload("evt_retry"))
    worker = _worker(
        postgres_session_factory,
        RecordingProcessor(exc=RuntimeError("boom")),
        owner_id="w1",
        retry_base_seconds=2.0,
    )
    await worker.run_once(now=T0)
    row = await _row(postgres_session_factory, "evt_retry")
    assert row.status == EventProcessStatus.FAILED.value
    assert row.attempt_count == 1
    assert row.next_attempt_at == T0 + timedelta(seconds=2)
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.last_error_code == "RuntimeError"
    assert row.result_summary == "RuntimeError: boom"


async def test_permanent_error_moves_to_dead(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_perm", payload=_payload("evt_perm"))
    worker = _worker(
        postgres_session_factory,
        RecordingProcessor(exc=EventPayloadError("bad payload")),
        owner_id="w1",
    )
    await worker.run_once(now=T0)
    row = await _row(postgres_session_factory, "evt_perm")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.next_attempt_at is None
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.last_error_code == "EventPayloadError"


async def test_corrupt_and_unsupported_payload_moves_to_dead(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_corrupt", payload={"garbage": 1})
    await _insert(
        postgres_session_factory,
        "evt_version",
        payload={"payload_version": 999, "event_id": "evt_version"},
    )
    worker = _worker(postgres_session_factory, RecordingProcessor(), owner_id="w1")
    await worker.run_once(now=T0)
    for event_id in ("evt_corrupt", "evt_version"):
        row = await _row(postgres_session_factory, event_id)
        assert row.status == EventProcessStatus.DEAD.value
        assert row.next_attempt_at is None
        assert row.last_error_code == "EventPayloadError"


async def test_worker_reaches_dead_after_max_attempts(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    await _insert(postgres_session_factory, "evt_ex", payload=_payload("evt_ex"))
    worker = _worker(
        postgres_session_factory,
        RecordingProcessor(exc=RuntimeError("always fails")),
        owner_id="w1",
        max_attempts=2,
        retry_base_seconds=2.0,
    )
    await worker.run_once(now=T0)
    row = await _row(postgres_session_factory, "evt_ex")
    assert row.status == EventProcessStatus.FAILED.value
    assert row.attempt_count == 1
    assert row.next_attempt_at == T0 + timedelta(seconds=2)

    later = T0 + timedelta(seconds=3)
    await worker.run_once(now=later)
    row = await _row(postgres_session_factory, "evt_ex")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.attempt_count == 2
    assert row.next_attempt_at is None
    assert row.lease_owner is None


async def test_business_idempotency_prevents_double_entry_on_retry(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    """A committed business write followed by a failed status must not duplicate
    the ledger entry when the worker retries: the unique
    ``(source_message_id, source_item_index)`` constraint turns the retry into
    an IntegrityError and the event lands in ``dead``.
    """

    class CommitThenFailProcessor:
        def __init__(self, factory: async_sessionmaker[Any]) -> None:
            self.factory = factory
            self.calls = 0

        async def process(self, event: dict[str, Any]) -> None:
            self.calls += 1
            async with self.factory() as session:
                session.add(
                    LedgerEntry(
                        user_open_id="ou_dup",
                        short_id="DUP01",
                        amount=Decimal("1.00"),
                        currency="CNY",
                        direction=Direction.EXPENSE,
                        category="餐饮",
                        note="",
                        occurred_at=T0,
                        source_type="text",
                        source_message_id="om_dup",
                        source_item_index=0,
                    )
                )
                await session.commit()
            if self.calls == 1:
                raise RuntimeError("reply failed after commit")

    await _insert(
        postgres_session_factory,
        "evt_dup",
        payload=_payload("evt_dup", message_id="om_dup"),
    )
    worker = _worker(
        postgres_session_factory,
        CommitThenFailProcessor(postgres_session_factory),
        owner_id="w1",
        retry_base_seconds=2.0,
    )
    await worker.run_once(now=T0)
    row = await _row(postgres_session_factory, "evt_dup")
    assert row.status == EventProcessStatus.FAILED.value

    later = T0 + timedelta(seconds=3)
    await worker.run_once(now=later)
    row = await _row(postgres_session_factory, "evt_dup")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.last_error_code == "IntegrityError"
    assert row.result_summary.startswith("IntegrityError")

    async with postgres_session_factory() as session:
        entries = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.user_open_id == "ou_dup")
                )
            )
            .scalars()
            .all()
        )
    assert len(entries) == 1


async def test_event_service_claim_deduplicates_on_postgres(
    postgres_session_factory: async_sessionmaker[Any],
) -> None:
    event = {
        "sender": {"sender_id": {"open_id": "ou_dup"}},
        "message": {
            "message_id": "om_claim",
            "message_type": "text",
            "content": '{"text":"hi"}',
        },
    }
    service = EventService(postgres_session_factory, RecordingProcessor(), worker_enabled=True)
    assert await service.claim("evt_claim", event, transport="webhook") is True
    assert await service.claim("evt_claim", event, transport="webhook") is False

    row = await _row(postgres_session_factory, "evt_claim")
    assert row.status == EventProcessStatus.RECEIVED.value
    assert row.attempt_count == 0
    assert row.source_message_id == "om_claim"
