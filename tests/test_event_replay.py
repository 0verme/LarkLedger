import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    REPLAY_SAFETY_VERSION,
    EventProcessStatus,
    build_stored_payload,
)
from lark_ledger.models import (
    Base,
    Direction,
    EventReplayAudit,
    LedgerEntry,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.outbox import ReplyStatus
from lark_ledger.services.event_replay import EventReplayService
from lark_ledger.services.worker import EventWorkerStore

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


def replayable_event(
    event_id: str,
    *,
    status: EventProcessStatus = EventProcessStatus.DEAD,
    source_message_id: str | None = None,
    message_type: str = "text",
    content: dict[str, object] | None = None,
    safety_version: int | None = REPLAY_SAFETY_VERSION,
    attempt_count: int = 3,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> ProcessedEvent:
    message_id = source_message_id or f"message-{event_id}"
    event = {
        "sender": {"sender_id": {"open_id": "user-private"}},
        "message": {
            "message_id": message_id,
            "message_type": message_type,
            "content": json.dumps(content or {"text": "private financial text"}),
        },
    }
    payload = build_stored_payload(
        event_id,
        event,
        transport="webhook",
        received_at=NOW - timedelta(minutes=10),
    )
    return ProcessedEvent(
        event_id=event_id,
        payload_json=payload,
        payload_version=PAYLOAD_VERSION,
        replay_safety_version=safety_version,
        status=status.value,
        attempt_count=attempt_count,
        manual_replay_count=0,
        source_message_id=message_id,
        user_open_id="user-private",
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        next_attempt_at=NOW + timedelta(hours=1),
        last_error_code="TemporaryFailure",
        result_summary="safe summary",
        updated_at=NOW - timedelta(minutes=1),
    )


async def audit_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(EventReplayAudit)) or 0)


async def test_dry_run_is_payload_free_and_does_not_modify_or_audit() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-dry"))
        await session.commit()

    result = await EventReplayService(factory).replay(
        "evt-dry",
        operator="operator-a",
        reason="temporary upstream outage",
        now=NOW,
    )

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt-dry")
        assert row is not None
        assert row.status == EventProcessStatus.DEAD.value
        assert row.attempt_count == 3
    output = json.dumps(result.to_safe_dict())
    assert result.mode == "dry-run"
    assert result.outcome == "eligible"
    assert result.preflight.eligible is True
    assert "private financial text" not in output
    assert "temporary upstream outage" not in output
    assert "operator-a" not in output
    assert await audit_count(factory) == 0
    await engine.dispose()


@pytest.mark.parametrize(
    ("operator", "reason", "message"),
    [
        ("", "reason", "operator is required"),
        ("operator", "", "reason is required"),
        ("x" * 129, "reason", "operator must be at most"),
        ("operator", "x" * 513, "reason must be at most"),
    ],
)
async def test_request_identity_and_reason_are_required_and_bounded(
    operator: str, reason: str, message: str
) -> None:
    engine, factory = await database()
    with pytest.raises(ValueError, match=message):
        await EventReplayService(factory).replay(
            "evt-validation", operator=operator, reason=reason, now=NOW
        )
    assert await audit_count(factory) == 0
    await engine.dispose()


@pytest.mark.parametrize(
    "status",
    [
        EventProcessStatus.SUCCEEDED,
        EventProcessStatus.LEGACY_SUCCEEDED,
        EventProcessStatus.RECEIVED,
    ],
)
async def test_closed_or_unprocessed_statuses_are_rejected(status: EventProcessStatus) -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-status", status=status))
        await session.commit()
    result = await EventReplayService(factory).replay(
        "evt-status", operator="operator", reason="investigation", now=NOW
    )
    assert result.outcome == "rejected"
    assert "status_not_replayable" in result.preflight.reason_codes
    await engine.dispose()


async def test_legacy_missing_payload_and_unproven_historical_contract_are_rejected() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id="evt-legacy",
                payload_json=None,
                payload_version=None,
                replay_safety_version=None,
                status=EventProcessStatus.DEAD.value,
                source_message_id="message-legacy",
            )
        )
        await session.commit()
    result = await EventReplayService(factory).replay(
        "evt-legacy", operator="operator", reason="investigation", now=NOW
    )
    assert {"payload_missing", "atomicity_unproven"}.issubset(
        result.preflight.reason_codes
    )
    await engine.dispose()


async def test_active_or_ambiguous_processing_lease_is_rejected_but_expired_is_eligible() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add_all(
            [
                replayable_event(
                    "evt-active",
                    status=EventProcessStatus.PROCESSING,
                    lease_owner="worker-a",
                    lease_expires_at=NOW + timedelta(minutes=1),
                ),
                replayable_event(
                    "evt-ambiguous",
                    status=EventProcessStatus.PROCESSING,
                    lease_owner="worker-b",
                ),
                replayable_event(
                    "evt-expired",
                    status=EventProcessStatus.PROCESSING,
                    lease_owner="worker-c",
                    lease_expires_at=NOW - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()
    service = EventReplayService(factory)
    active = await service.preflight("evt-active", now=NOW)
    ambiguous = await service.preflight("evt-ambiguous", now=NOW)
    expired = await service.preflight("evt-expired", now=NOW)
    assert active.eligible is False and active.lease_state == "active"
    assert ambiguous.eligible is False and ambiguous.lease_state == "ambiguous"
    assert expired.eligible is True and expired.lease_state == "expired"
    await engine.dispose()


@pytest.mark.parametrize(
    "outbox_status",
    [ReplyStatus.SENT, ReplyStatus.PENDING, ReplyStatus.FAILED, ReplyStatus.DEAD],
)
async def test_any_existing_outbox_refuses_business_replay_and_points_to_result_replay(
    outbox_status: ReplyStatus,
) -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-outbox"))
        session.add(
            ReplyOutbox(
                event_id="evt-outbox",
                message_id="message-evt-outbox",
                reply_type="text",
                payload_json={"text": "existing result"},
                status=outbox_status.value,
            )
        )
        await session.commit()
    result = await EventReplayService(factory).replay(
        "evt-outbox", operator="operator", reason="investigation", now=NOW
    )
    assert result.outcome == "rejected"
    assert result.preflight.recommended_action == "replay_result"
    assert result.preflight.outbox_statuses == (outbox_status.value,)
    await engine.dispose()


async def test_existing_source_business_result_refuses_replay_and_detects_batch() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-business", source_message_id="source-business"))
        for index in (0, 1):
            session.add(
                LedgerEntry(
                    user_open_id="user-private",
                    short_id=f"SAFE{index}",
                    amount=Decimal("10.00"),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="test",
                    note="existing",
                    occurred_at=NOW,
                    source_message_id="source-business",
                    source_item_index=index,
                )
            )
        await session.commit()
    result = await EventReplayService(factory).replay(
        "evt-business", operator="operator", reason="investigation", now=NOW
    )
    assert result.preflight.eligible is False
    assert result.preflight.ledger_entry_count == 2
    assert result.preflight.batch_risk == "confirmed_existing_batch_result"
    assert result.preflight.recommended_action == "investigate_duplicate_business_risk"
    await engine.dispose()


async def test_durable_business_commit_proof_refuses_replay_after_outbox_cleanup() -> None:
    engine, factory = await database()
    async with factory() as session:
        row = replayable_event("evt-committed")
        row.business_committed_at = NOW - timedelta(days=31)
        session.add(row)
        await session.commit()
    result = await EventReplayService(factory).replay(
        "evt-committed", operator="operator", reason="investigation", now=NOW
    )
    assert result.outcome == "rejected"
    assert result.preflight.outbox_count == 0
    assert result.preflight.business_result_committed is True
    assert "business_result_committed" in result.preflight.reason_codes
    assert result.preflight.recommended_action == "investigate_duplicate_business_risk"
    await engine.dispose()


async def test_safe_execute_resets_state_and_writes_audit_in_same_transaction() -> None:
    engine, factory = await database()
    async with factory() as session:
        row = replayable_event("evt-execute", attempt_count=7)
        row.manual_replay_count = 2
        session.add(row)
        await session.commit()

    result = await EventReplayService(factory).replay(
        "evt-execute",
        operator="operator-a",
        reason="temporary AI outage",
        execute=True,
        now=NOW,
    )

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt-execute")
        audit = await session.scalar(select(EventReplayAudit))
        assert row is not None and audit is not None
        assert row.status == EventProcessStatus.RECEIVED.value
        assert row.attempt_count == 0
        assert row.manual_replay_count == 3
        assert row.next_attempt_at is None
        assert row.lease_owner is None
        assert row.lease_expires_at is None
        assert row.last_error_code is None
        assert row.result_summary is None
        assert audit.id == result.audit_id
        assert audit.previous_status == EventProcessStatus.DEAD.value
        assert audit.previous_attempt_count == 7
        assert audit.replay_number == 3
        assert audit.outcome == "requeued"
        assert audit.operator == "operator-a"
        assert audit.reason == "temporary AI outage"
    await engine.dispose()


async def test_audit_creation_failure_rolls_back_event_reset() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-rollback", attempt_count=4))
        await session.commit()

    class BrokenAuditService(EventReplayService):
        @staticmethod
        def _new_audit(**_kwargs: object) -> EventReplayAudit:
            raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await BrokenAuditService(factory).replay(
            "evt-rollback",
            operator="operator",
            reason="temporary failure",
            execute=True,
            now=NOW,
        )

    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt-rollback")
        assert row is not None
        assert row.status == EventProcessStatus.DEAD.value
        assert row.attempt_count == 4
        assert row.manual_replay_count == 0
    assert await audit_count(factory) == 0
    await engine.dispose()


async def test_replayed_event_gets_a_fresh_bounded_worker_attempt_window() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-worker", attempt_count=99))
        await session.commit()
    await EventReplayService(factory).replay(
        "evt-worker",
        operator="operator",
        reason="dependency recovered",
        execute=True,
        now=NOW,
    )

    claimed = await EventWorkerStore(factory).claim_batch(
        "worker-new", NOW, batch_size=1, lease_seconds=60
    )
    assert len(claimed) == 1
    assert claimed[0].event_id == "evt-worker"
    assert claimed[0].attempt_count == 1
    async with factory() as session:
        row = await session.get(ProcessedEvent, "evt-worker")
        assert row is not None
        assert row.manual_replay_count == 1
        assert row.attempt_count == 1
    await engine.dispose()


async def test_visual_and_multiline_text_events_report_batch_risk_without_payload() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-image", message_type="image"))
        session.add(
            replayable_event(
                "evt-lines",
                content={"text": "first private line\nsecond private line"},
            )
        )
        await session.commit()
    service = EventReplayService(factory)
    image = await service.preflight("evt-image", now=NOW)
    lines = await service.preflight("evt-lines", now=NOW)
    assert image.batch_risk == "possible_batch"
    assert lines.batch_risk == "possible_batch"
    assert "private" not in json.dumps(image.to_safe_dict())
    assert "private" not in json.dumps(lines.to_safe_dict())
    await engine.dispose()


async def test_second_operator_execute_after_requeue_is_rejected() -> None:
    engine, factory = await database()
    async with factory() as session:
        session.add(replayable_event("evt-two"))
        await session.commit()
    service = EventReplayService(factory)
    first = await service.replay(
        "evt-two", operator="operator-a", reason="reason-a", execute=True, now=NOW
    )
    second = await service.replay(
        "evt-two", operator="operator-b", reason="reason-b", execute=True, now=NOW
    )
    assert sorted([first.outcome, second.outcome]) == ["rejected", "requeued"]
    assert await audit_count(factory) == 2
    await engine.dispose()
