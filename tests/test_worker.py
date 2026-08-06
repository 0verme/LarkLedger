"""P05b: event worker, lease, retry, and dead-letter handling.

SQLite in-memory mirrors the PostgreSQL claim/lease/retry state machine for the
single-connection case; PostgreSQL integration tests in
``tests/integration/test_worker_postgres.py`` exercise the real ``SKIP LOCKED``
concurrency and lease semantics. All worker behavior here uses injected clocks,
sleepers, owner IDs, stores, and processors — no real time or network.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.event_payload import (
    EventPayloadError,
    EventProcessStatus,
    build_stored_payload,
    serialize_payload,
)
from lark_ledger.models import Base, ProcessedEvent
from lark_ledger.services.events import EventService
from lark_ledger.services.worker import (
    ClaimedEvent,
    EventWorker,
    EventWorkerStore,
    compute_retry_delay_seconds,
    failure_status,
    generate_owner_id,
    is_permanent_error,
    safe_owner_id,
    schedule_next_attempt,
)

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


class RecordingProcessor:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.exc = exc

    async def process(self, event: dict[str, Any]) -> None:
        if self.exc is not None:
            raise self.exc
        self.events.append(event)


def _sample_event(message_id: str = "om_1") -> dict[str, Any]:
    return {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": '{"text":"hi"}',
        },
    }


def _payload(event_id: str, message_id: str = "om_1") -> dict[str, Any]:
    return serialize_payload(
        build_stored_payload(
            event_id, _sample_event(message_id), transport="webhook", received_at=T0
        )
    )


def _invalid_version_payload(event_id: str) -> dict[str, Any]:
    return {"payload_version": 999, "event_id": event_id}


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


async def _row(factory: async_sessionmaker[Any], event_id: str) -> ProcessedEvent:
    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None
        return row


def _store(factory: async_sessionmaker[Any]) -> EventWorkerStore:
    return EventWorkerStore(factory)


def _worker(
    factory: async_sessionmaker[Any],
    processor: RecordingProcessor,
    *,
    owner_id: str = "test-worker",
    **kwargs: Any,
) -> EventWorker:
    return EventWorker(
        _store(factory),
        processor,
        owner_id=owner_id,
        jitter=None,
        **kwargs,
    )


def _naive(value: datetime) -> datetime:
    """SQLite returns stored datetimes as naive; drop tz for comparisons."""
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


# ---------------------------------------------------------------------------
# Pure helpers: backoff, scheduling, classification, owner identity
# ---------------------------------------------------------------------------


def test_exponential_backoff_formula() -> None:
    assert compute_retry_delay_seconds(1, base_seconds=2.0, max_seconds=3600.0) == 2.0
    assert compute_retry_delay_seconds(2, base_seconds=2.0, max_seconds=3600.0) == 4.0
    assert compute_retry_delay_seconds(3, base_seconds=2.0, max_seconds=3600.0) == 8.0


def test_backoff_is_capped_at_max() -> None:
    assert compute_retry_delay_seconds(5, base_seconds=2.0, max_seconds=10.0) == 10.0
    assert compute_retry_delay_seconds(20, base_seconds=2.0, max_seconds=10.0) == 10.0


def test_backoff_clamps_attempt_to_at_least_one() -> None:
    assert compute_retry_delay_seconds(0, base_seconds=2.0, max_seconds=3600.0) == 2.0


def test_schedule_next_attempt_is_timezone_aware_and_jitterable() -> None:
    assert schedule_next_attempt(T0, 1, base_seconds=2.0, max_seconds=3600.0) == T0 + timedelta(
        seconds=2
    )
    assert schedule_next_attempt(T0, 3, base_seconds=2.0, max_seconds=3600.0) == T0 + timedelta(
        seconds=8
    )
    assert (
        schedule_next_attempt(
            T0, 1, base_seconds=2.0, max_seconds=3600.0, jitter=lambda delay: delay * 0.5
        )
        == T0 + timedelta(seconds=1)
    )


def test_failure_status_decides_failed_vs_dead() -> None:
    assert failure_status(1, max_attempts=3, permanent=False) == EventProcessStatus.FAILED.value
    assert failure_status(3, max_attempts=3, permanent=False) == EventProcessStatus.DEAD.value
    assert failure_status(1, max_attempts=3, permanent=True) == EventProcessStatus.DEAD.value
    assert failure_status(0, max_attempts=3, permanent=False) == EventProcessStatus.FAILED.value


def test_permanent_error_classification_is_explicit() -> None:
    assert is_permanent_error(EventPayloadError("bad payload")) is True
    assert is_permanent_error(ValueError("contract")) is True
    assert is_permanent_error(TypeError("type")) is True
    assert (
        is_permanent_error(IntegrityError("stmt", {}, RuntimeError("dup"))) is True
    )

    request = httpx.Request("POST", "https://example.com")
    for code in (400, 401, 403, 404, 422):
        err = httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(code, request=request)
        )
        assert is_permanent_error(err) is True, f"4xx {code} should be permanent"
    for code in (408, 429, 500, 502, 503):
        err = httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(code, request=request)
        )
        assert is_permanent_error(err) is False, f"{code} should be retryable"


def test_unknown_errors_default_to_retryable() -> None:
    assert is_permanent_error(RuntimeError("transient")) is False
    assert is_permanent_error(ConnectionError("down")) is False
    assert is_permanent_error(httpx.TimeoutException("slow")) is False
    assert is_permanent_error(OSError("temporary")) is False


def test_owner_id_and_safe_label() -> None:
    owner = generate_owner_id()
    parts = owner.split(":")
    assert len(parts) == 3
    assert safe_owner_id(owner) == f"{parts[0]}:{parts[1]}"
    assert safe_owner_id("just-a-name") == "just-a-name"
    assert ":" not in safe_owner_id("just-a-name")


# ---------------------------------------------------------------------------
# Store: claim conditions
# ---------------------------------------------------------------------------


async def test_claim_picks_received_event_and_sets_lease() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_1", payload=_payload("evt_1"))

    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert claimed == [ClaimedEvent(event_id="evt_1", attempt_count=1)]

    row = await _row(factory, "evt_1")
    assert row.status == EventProcessStatus.PROCESSING.value
    assert row.attempt_count == 1
    assert row.lease_owner == "w1"
    assert row.lease_expires_at == _naive(T0 + timedelta(seconds=300))
    assert row.next_attempt_at is None
    await engine.dispose()


async def test_claim_condition_matrix() -> None:
    engine, factory = await _sqlite_factory()
    future = T0 + timedelta(hours=1)
    past = T0 - timedelta(hours=1)
    await _insert(factory, "recv", payload=_payload("recv"))
    await _insert(
        factory,
        "failed_due",
        status=EventProcessStatus.FAILED.value,
        attempt_count=1,
        payload=_payload("failed_due"),
        next_attempt_at=past,
    )
    await _insert(
        factory,
        "failed_future",
        status=EventProcessStatus.FAILED.value,
        attempt_count=1,
        payload=_payload("failed_future"),
        next_attempt_at=future,
    )
    await _insert(
        factory,
        "proc_active",
        status=EventProcessStatus.PROCESSING.value,
        attempt_count=1,
        payload=_payload("proc_active"),
        lease_owner="other",
        lease_expires_at=future,
    )
    await _insert(
        factory,
        "proc_expired",
        status=EventProcessStatus.PROCESSING.value,
        attempt_count=1,
        payload=_payload("proc_expired"),
        lease_owner="old-worker",
        lease_expires_at=past,
    )
    await _insert(
        factory, "done", status=EventProcessStatus.SUCCEEDED.value, payload=_payload("done")
    )
    await _insert(
        factory, "dead", status=EventProcessStatus.DEAD.value, payload=_payload("dead")
    )
    await _insert(
        factory,
        "legacy",
        status=EventProcessStatus.LEGACY_SUCCEEDED.value,
        payload=None,
    )

    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    claimed_ids = {item.event_id for item in claimed}

    assert "recv" in claimed_ids
    assert "failed_due" in claimed_ids
    assert "failed_future" not in claimed_ids
    assert "proc_active" not in claimed_ids
    assert "proc_expired" in claimed_ids
    assert "done" not in claimed_ids
    assert "dead" not in claimed_ids
    assert "legacy" not in claimed_ids

    # The expired-processing reclaim is the second attempt for that event.
    attempts = {item.event_id: item.attempt_count for item in claimed}
    assert attempts["proc_expired"] == 2
    assert attempts["recv"] == 1
    await engine.dispose()


async def test_claim_skips_payload_null_legacy_row() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(
        factory,
        "legacy",
        status=EventProcessStatus.LEGACY_SUCCEEDED.value,
        payload=None,
    )
    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert claimed == []
    await engine.dispose()


async def test_claim_batch_respects_batch_size_and_orders_by_received_at() -> None:
    engine, factory = await _sqlite_factory()
    for index, event_id in enumerate(["e1", "e2", "e3"]):
        await _insert(
            factory,
            event_id,
            payload=_payload(event_id),
            received_at=T0 + timedelta(minutes=index),
        )
    claimed = await _store(factory).claim_batch("w1", T0, batch_size=2, lease_seconds=300.0)
    assert [item.event_id for item in claimed] == ["e1", "e2"]
    await engine.dispose()


async def test_claim_is_idempotent_within_one_sweep() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_once", payload=_payload("evt_once"))
    store = _store(factory)
    first = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    second = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert len(first) == 1
    assert second == []
    row = await _row(factory, "evt_once")
    assert row.attempt_count == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# Lease semantics
# ---------------------------------------------------------------------------


async def test_lease_guards_completion() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_lease", payload=_payload("evt_lease"))
    store = _store(factory)
    assert await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)

    # A different owner cannot complete the event.
    assert await store.complete("evt_lease", "w2", T0) is False
    row = await _row(factory, "evt_lease")
    assert row.status == EventProcessStatus.PROCESSING.value
    assert row.lease_owner == "w1"

    # The current owner can.
    assert await store.complete("evt_lease", "w1", T0) is True
    row = await _row(factory, "evt_lease")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.next_attempt_at is None
    assert row.last_error_code is None
    assert row.result_summary is None
    await engine.dispose()


async def test_expired_lease_reclaim_and_stale_worker_cannot_overwrite() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_reclaim", payload=_payload("evt_reclaim"))
    store = _store(factory)

    # Worker A claims at T0.
    assert await store.claim_batch("worker-a", T0, batch_size=10, lease_seconds=300.0)
    # Worker B reclaims the same row after A's lease expired (now = T0 + 301s).
    later = T0 + timedelta(seconds=301)
    reclaimed = await store.claim_batch("worker-b", later, batch_size=10, lease_seconds=300.0)
    assert [item.event_id for item in reclaimed] == ["evt_reclaim"]
    assert reclaimed[0].attempt_count == 2

    # Stale worker A cannot overwrite B's ownership.
    assert await store.complete("evt_reclaim", "worker-a", later) is False
    assert await store.record_failure(
        "evt_reclaim",
        "worker-a",
        status=EventProcessStatus.DEAD.value,
        next_attempt_at=None,
        error_code="X",
        summary="stale",
        now=later,
    ) is False

    # New owner B completes the event.
    assert await store.complete("evt_reclaim", "worker-b", later) is True
    row = await _row(factory, "evt_reclaim")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.lease_owner is None
    await engine.dispose()


async def test_failure_clears_lease_fields() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_fail_lease", payload=_payload("evt_fail_lease"))
    store = _store(factory)
    assert await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)

    next_at = T0 + timedelta(seconds=30)
    recorded = await store.record_failure(
        "evt_fail_lease",
        "w1",
        status=EventProcessStatus.FAILED.value,
        next_attempt_at=next_at,
        error_code="RuntimeError",
        summary="RuntimeError: boom",
        now=T0,
    )
    assert recorded is True
    row = await _row(factory, "evt_fail_lease")
    assert row.status == EventProcessStatus.FAILED.value
    assert row.next_attempt_at == _naive(next_at)
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.last_error_code == "RuntimeError"
    assert row.result_summary == "RuntimeError: boom"
    await engine.dispose()


# ---------------------------------------------------------------------------
# Worker orchestration: attempt counting, retry, dead, loop robustness
# ---------------------------------------------------------------------------


async def test_worker_processes_received_event_to_succeeded() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_ok", payload=_payload("evt_ok"))
    processor = RecordingProcessor()
    worker = _worker(factory, processor, owner_id="w1")
    count = await worker.run_once(now=T0)
    assert count == 1
    assert len(processor.events) == 1
    row = await _row(factory, "evt_ok")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.attempt_count == 1
    assert row.lease_owner is None
    await engine.dispose()


async def test_worker_retryable_failure_schedules_next_attempt() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_retry", payload=_payload("evt_retry"))
    processor = RecordingProcessor(exc=RuntimeError("network hiccup"))
    worker = _worker(factory, processor, owner_id="w1", retry_base_seconds=2.0)
    await worker.run_once(now=T0)
    row = await _row(factory, "evt_retry")
    assert row.status == EventProcessStatus.FAILED.value
    assert row.attempt_count == 1
    assert row.next_attempt_at == _naive(T0 + timedelta(seconds=2))
    assert row.lease_owner is None
    assert row.last_error_code == "RuntimeError"
    assert row.result_summary == "RuntimeError: network hiccup"
    await engine.dispose()


async def test_worker_permanent_error_moves_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_perm", payload=_payload("evt_perm"))
    processor = RecordingProcessor(exc=EventPayloadError("bad payload"))
    worker = _worker(factory, processor, owner_id="w1")
    await worker.run_once(now=T0)
    row = await _row(factory, "evt_perm")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.attempt_count == 1
    assert row.next_attempt_at is None
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.last_error_code == "EventPayloadError"
    await engine.dispose()


async def test_worker_corrupt_payload_moves_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_bad", payload={"garbage": True})
    worker = _worker(factory, RecordingProcessor(), owner_id="w1")
    await worker.run_once(now=T0)
    row = await _row(factory, "evt_bad")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.attempt_count == 1
    assert row.next_attempt_at is None
    await engine.dispose()


async def test_worker_unsupported_payload_version_moves_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_version", payload=_invalid_version_payload("evt_version"))
    worker = _worker(factory, RecordingProcessor(), owner_id="w1")
    await worker.run_once(now=T0)
    row = await _row(factory, "evt_version")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.last_error_code == "EventPayloadError"
    assert row.next_attempt_at is None
    await engine.dispose()


async def test_worker_reaches_dead_after_max_attempts() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_exhaust", payload=_payload("evt_exhaust"))
    processor = RecordingProcessor(exc=RuntimeError("always fails"))
    worker = _worker(factory, processor, owner_id="w1", max_attempts=2, retry_base_seconds=2.0)

    await worker.run_once(now=T0)
    row = await _row(factory, "evt_exhaust")
    assert row.status == EventProcessStatus.FAILED.value
    assert row.attempt_count == 1
    assert row.next_attempt_at == _naive(T0 + timedelta(seconds=2))

    # Retry after the scheduled time; this is the final attempt and it fails.
    later = T0 + timedelta(seconds=3)
    await worker.run_once(now=later)
    row = await _row(factory, "evt_exhaust")
    assert row.status == EventProcessStatus.DEAD.value
    assert row.attempt_count == 2
    assert row.next_attempt_at is None
    assert row.lease_owner is None
    await engine.dispose()


async def test_single_event_failure_does_not_kill_worker_sweep() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_a", payload=_payload("evt_a"))
    await _insert(factory, "evt_b", payload=_payload("evt_b", message_id="om_b"))

    class FailOnceProcessor:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.failed = False

        async def process(self, event: dict[str, Any]) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("transient")
            self.events.append(str(event["message"]["message_id"]))

    worker = _worker(factory, FailOnceProcessor(), owner_id="w1", retry_base_seconds=2.0)
    count = await worker.run_once(now=T0)  # must not raise
    assert count == 2
    a = await _row(factory, "evt_a")
    b = await _row(factory, "evt_b")
    assert a.status == EventProcessStatus.FAILED.value
    assert b.status == EventProcessStatus.SUCCEEDED.value
    await engine.dispose()


async def test_worker_stop_prevents_new_claims() -> None:
    engine, factory = await _sqlite_factory()
    await _insert(factory, "evt_stop", payload=_payload("evt_stop"))
    worker = _worker(factory, RecordingProcessor(), owner_id="w1")
    worker._stop.set()
    count = await worker.run_once(now=T0)
    assert count == 0
    row = await _row(factory, "evt_stop")
    assert row.status == EventProcessStatus.RECEIVED.value
    assert row.attempt_count == 0
    await engine.dispose()


async def test_worker_start_stop_leaves_no_dangling_task() -> None:
    engine, factory = await _sqlite_factory()
    # A long sleeper keeps the loop parked in ``asyncio.sleep`` between sweeps so
    # cancellation never interrupts a live SQLite session (which would tear down
    # the in-memory StaticPool connection under aiosqlite).
    worker = _worker(
        factory,
        RecordingProcessor(),
        owner_id="w1",
        sleeper=lambda _delay: asyncio.sleep(3600),
    )
    try:
        worker.start()
        assert worker.running is True
        await asyncio.sleep(0)  # let the loop run its first (empty) sweep and sleep
    finally:
        await worker.stop()
    assert worker.running is False
    pending = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "lark-ledger-event-worker"
    ]
    assert pending == []
    await engine.dispose()


# ---------------------------------------------------------------------------
# EventService entry routing (worker vs sync)
# ---------------------------------------------------------------------------


async def test_handle_safely_claims_only_when_worker_enabled() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    service = EventService(factory, processor, worker_enabled=True)

    await service.handle_safely("evt_ws", _sample_event(), transport="webhook")
    assert processor.events == []  # worker owns processing
    row = await _row(factory, "evt_ws")
    assert row.status == EventProcessStatus.RECEIVED.value
    assert row.attempt_count == 0
    assert row.payload_json is not None

    # Duplicate delivery still dedups without reprocessing.
    await service.handle_safely("evt_ws", _sample_event(), transport="webhook")
    row = await _row(factory, "evt_ws")
    assert row.status == EventProcessStatus.RECEIVED.value
    assert row.attempt_count == 0
    await engine.dispose()


async def test_handle_safely_processes_synchronously_when_worker_disabled() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    service = EventService(factory, processor, worker_enabled=False)

    await service.handle_safely("evt_sync", _sample_event(), transport="webhook")
    assert len(processor.events) == 1
    row = await _row(factory, "evt_sync")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.attempt_count == 1
    await engine.dispose()


async def test_claim_method_returns_false_for_duplicate() -> None:
    engine, factory = await _sqlite_factory()
    service = EventService(factory, RecordingProcessor(), worker_enabled=True)
    assert await service.claim("evt_dup", _sample_event(), transport="webhook") is True
    assert await service.claim("evt_dup", _sample_event(), transport="webhook") is False

    async with factory() as session:
        rows = (await session.execute(select(ProcessedEvent))).scalars().all()
    assert len(rows) == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_worker_config_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.worker_enabled is True
    assert settings.worker_poll_interval_seconds == 1.0
    assert settings.worker_batch_size == 10
    assert settings.event_max_attempts == 3
    assert settings.event_lease_seconds == 300.0
    assert settings.event_retry_base_seconds == 2.0
    assert settings.event_retry_max_seconds == 3600.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"worker_poll_interval_seconds": 0},
        {"worker_batch_size": 0},
        {"event_max_attempts": 0},
        {"event_lease_seconds": 0},
        {"event_retry_base_seconds": 0},
    ],
)
def test_worker_config_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)
