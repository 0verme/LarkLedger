"""P05a: reliable-delivery event state model (state fields, transitions, safety).

SQLite in-memory mirrors PostgreSQL semantics except that SQLite returns naive
datetime objects; timezone-awareness of stored timestamps is asserted in the
PostgreSQL integration suite.
"""

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.event_payload import (
    MAX_RESULT_SUMMARY_LENGTH,
    TERMINAL_STATUSES,
    WORKER_CLAIMABLE_STATUSES,
    EventProcessStatus,
    is_replayable_payload,
    safe_error_summary,
    user_open_id_from_event,
)
from lark_ledger.models import Base, ProcessedEvent
from lark_ledger.services.events import EventService


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_once = False

    async def process(self, event: dict[str, Any]) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated processor failure")
        self.events.append(event)


def sample_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": "om_text",
            "message_type": "text",
            "content": json.dumps({"text": "午饭32元"}, ensure_ascii=False),
        },
    }
    event.update(overrides)
    return event


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _fresh_row(factory: async_sessionmaker[Any], event_id: str) -> ProcessedEvent:
    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None
        return row


async def test_new_claim_records_state_fields() -> None:
    engine, factory = await _sqlite_factory()
    service = EventService(factory, RecordingProcessor())
    assert await service.handle("evt_new", sample_event(), transport="webhook")

    row = await _fresh_row(factory, "evt_new")
    assert row.status == EventProcessStatus.SUCCEEDED.value
    assert row.attempt_count == 1  # one processing attempt was started
    assert row.source_message_id == "om_text"
    assert row.user_open_id == "ou_user"
    assert row.next_attempt_at is None
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.result_summary is None
    assert row.last_error_code is None
    assert row.received_at is not None
    assert row.updated_at is not None

    await engine.dispose()


async def test_model_defaults_for_initial_event() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        session.add(ProcessedEvent(event_id="evt_defaults"))
        await session.commit()

    row = await _fresh_row(factory, "evt_defaults")
    assert row.status == EventProcessStatus.RECEIVED.value
    assert row.attempt_count == 0
    assert row.payload_json is None
    assert row.source_message_id is None
    assert row.user_open_id is None

    await engine.dispose()


async def test_duplicate_delivery_does_not_reprocess_or_increment_attempt() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    service = EventService(factory, processor)

    assert await service.handle("evt_dup", sample_event(), transport="webhook")
    assert not await service.handle("evt_dup", sample_event(), transport="webhook")
    assert len(processor.events) == 1
    assert (await _fresh_row(factory, "evt_dup")).attempt_count == 1

    await engine.dispose()


async def test_failure_records_safe_summary_and_counts_attempt() -> None:
    engine, factory = await _sqlite_factory()
    processor = RecordingProcessor()
    processor.fail_once = True
    service = EventService(factory, processor)

    with pytest.raises(RuntimeError, match="simulated processor failure"):
        await service.handle("evt_fail", sample_event(), transport="webhook")

    # Already claimed: second delivery must not re-run the processor.
    assert not await service.handle("evt_fail", sample_event(), transport="webhook")
    assert processor.events == []

    row = await _fresh_row(factory, "evt_fail")
    assert row.status == EventProcessStatus.FAILED.value
    assert row.attempt_count == 1
    assert row.last_error_code == "RuntimeError"
    assert row.result_summary == "RuntimeError: simulated processor failure"
    assert row.next_attempt_at is None  # no retry scheduler in this version
    assert row.payload_json is not None

    await engine.dispose()


async def test_legacy_row_remains_non_replayable_with_zero_attempts() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id="evt_legacy",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
                payload_json=None,
            )
        )
        await session.commit()

    row = await _fresh_row(factory, "evt_legacy")
    assert row.status == EventProcessStatus.LEGACY_SUCCEEDED.value
    assert row.payload_json is None
    assert row.attempt_count == 0
    assert row.source_message_id is None
    assert row.user_open_id is None
    assert not is_replayable_payload(row.payload_json)

    await engine.dispose()


async def test_user_open_id_denormalization_prefers_open_id() -> None:
    assert (
        user_open_id_from_event(
            {"sender": {"sender_id": {"open_id": "ou_1", "user_id": "uid_1"}}}
        )
        == "ou_1"
    )
    assert (
        user_open_id_from_event({"sender": {"sender_id": {"user_id": "uid_1"}}})
        == "uid_1"
    )
    assert user_open_id_from_event({"sender": {"sender_id": {}}}) is None
    assert user_open_id_from_event({"sender": {}}) is None
    assert user_open_id_from_event({}) is None


def test_status_enum_is_closed_and_classified() -> None:
    assert {member.value for member in EventProcessStatus} == {
        "received",
        "processing",
        "succeeded",
        "failed",
        "dead",
        "legacy_succeeded",
    }
    # Terminal states are never picked up again.
    assert TERMINAL_STATUSES == {
        EventProcessStatus.SUCCEEDED.value,
        EventProcessStatus.DEAD.value,
        EventProcessStatus.LEGACY_SUCCEEDED.value,
    }
    # Future worker may claim received and failed (subject to retry/lease windows).
    assert WORKER_CLAIMABLE_STATUSES == {
        EventProcessStatus.RECEIVED.value,
        EventProcessStatus.FAILED.value,
    }
    assert EventProcessStatus.PROCESSING.value not in WORKER_CLAIMABLE_STATUSES


async def test_status_guard_rejects_non_enum_values() -> None:
    engine, factory = await _sqlite_factory()
    service = EventService(factory, RecordingProcessor())
    with pytest.raises(TypeError, match="EventProcessStatus"):
        await service._mark_status("evt_guard", "bogus_status")  # type: ignore[arg-type]
    await engine.dispose()


def test_error_summary_is_single_line_and_capped() -> None:
    long_error = RuntimeError("x" * 2000)
    summary = safe_error_summary(long_error)
    assert len(summary) <= MAX_RESULT_SUMMARY_LENGTH
    assert summary.endswith("…")

    multiline = RuntimeError("first line\nsecond line\nTraceback (most recent call last)")
    one = safe_error_summary(multiline)
    assert one.startswith("RuntimeError: first line")
    assert "second line" not in one
    assert "Traceback" not in one

    bare = RuntimeError()
    assert safe_error_summary(bare) == "RuntimeError"


def test_error_summary_survives_broken_str() -> None:
    class BrokenStrError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("__str__ failed")

    # The summary must never mask the failure it is recording.
    assert safe_error_summary(BrokenStrError()) == "BrokenStrError"


def test_error_summary_redacts_credentials() -> None:
    with_password = safe_error_summary(
        RuntimeError("connect failed: postgresql+asyncpg://user:secret@localhost:5432/db")
    )
    assert "secret" not in with_password
    assert "user:***@" in with_password

    with_bearer = safe_error_summary(RuntimeError("AI rejected bearer sk-1234567890abc"))
    assert "sk-1234567890abc" not in with_bearer
    assert "bearer ***" in with_bearer

    with_auth = safe_error_summary(RuntimeError("Authorization: Bearer abc-def"))
    assert "abc-def" not in with_auth
    assert "Authorization: ***" in with_auth

    plain = safe_error_summary(RuntimeError("https://api.example.com/v1 returned 400"))
    assert plain == "RuntimeError: https://api.example.com/v1 returned 400"


async def test_timestamps_are_recorded_for_claim() -> None:
    engine, factory = await _sqlite_factory()
    service = EventService(factory, RecordingProcessor())
    assert await service.handle("evt_ts", sample_event(), transport="websocket")
    row = await _fresh_row(factory, "evt_ts")
    # The application writes timezone-aware datetimes; SQLite returns them naive,
    # so PostgreSQL asserts awareness in the integration suite.
    assert row.received_at is not None
    assert row.updated_at is not None

    await engine.dispose()
