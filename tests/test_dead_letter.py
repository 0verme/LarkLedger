"""Unit tests for the dead-letter domain vocabulary and query/ops services (P44)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.dead_letter import (
    RETRYABLE_REASONS,
    TERMINAL_REASONS,
    DeadLetterReason,
    DeadLetterSource,
    assessment_for,
    classify_error_code,
)
from lark_ledger.models import Base, DeadLetterAction, ProcessedEvent, ReplyOutbox
from lark_ledger.outbox import ReplyStatus
from lark_ledger.services.dead_letter import (
    DeadLetterConflictError,
    DeadLetterNotFoundError,
    DeadLetterOpsService,
    DeadLetterQueryService,
)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    def test_http_400_is_remote_rejected(self) -> None:
        reason = classify_error_code(
            "HTTPStatusError",
            "HTTPStatusError: Client error '400 Bad Request' for url "
            "'https://open.feishu.cn/open-apis/im/v1/messages/om_accept/reply'",
        )
        assert reason is DeadLetterReason.REMOTE_REJECTED

    def test_http_429_is_rate_limited(self) -> None:
        reason = classify_error_code(
            "HTTPStatusError",
            "HTTPStatusError: Client error '429 Too Many Requests' for url 'https://x'",
        )
        assert reason is DeadLetterReason.RATE_LIMITED

    def test_http_502_is_retryable_remote(self) -> None:
        reason = classify_error_code(
            "HTTPStatusError",
            "HTTPStatusError: Server error '502 Bad Gateway' for url 'https://x'",
        )
        assert reason is DeadLetterReason.NETWORK
        assert reason in RETRYABLE_REASONS
        assert reason not in TERMINAL_REASONS

    def test_http_401_and_403(self) -> None:
        assert classify_error_code("HTTPStatusError", "Client error '401 Unauthorized'") is (
            DeadLetterReason.AUTHENTICATION
        )
        assert classify_error_code("HTTPStatusError", "Client error '403 Forbidden'") is (
            DeadLetterReason.PERMISSION
        )
        assert classify_error_code("HTTPStatusError", "Client error '404 Not Found'") is (
            DeadLetterReason.REMOTE_NOT_FOUND
        )

    def test_timeout_and_network(self) -> None:
        assert classify_error_code("ReadTimeout", None) is DeadLetterReason.TIMEOUT
        assert classify_error_code("ConnectError", None) is DeadLetterReason.NETWORK
        assert classify_error_code("TimeoutError", None) is DeadLetterReason.TIMEOUT

    def test_payload_and_database(self) -> None:
        assert classify_error_code("ReplyPayloadError", None) is (DeadLetterReason.INVALID_PAYLOAD)
        assert classify_error_code("EventPayloadError", None) is (DeadLetterReason.INVALID_PAYLOAD)
        assert classify_error_code("IntegrityError", None) is DeadLetterReason.DATABASE

    def test_unknown_and_empty(self) -> None:
        assert classify_error_code("MysteryException", "boom") is DeadLetterReason.UNKNOWN
        assert classify_error_code(None, None) is DeadLetterReason.UNKNOWN
        assert classify_error_code("", "") is DeadLetterReason.UNKNOWN


class TestAssessment:
    def test_terminal_reason_not_replayable(self) -> None:
        assessment = assessment_for(
            source=DeadLetterSource.OUTBOX,
            status="dead",
            reason=DeadLetterReason.REMOTE_REJECTED,
            attempts=1,
        )
        assert assessment.terminal
        assert not assessment.replay_safe
        assert not assessment.retryable

    def test_transient_is_replay_safe(self) -> None:
        assessment = assessment_for(
            source=DeadLetterSource.OUTBOX,
            status="failed",
            reason=DeadLetterReason.TIMEOUT,
            attempts=1,
        )
        assert assessment.retryable
        assert assessment.replay_safe

    def test_transient_with_remote_message_id_is_unsafe(self) -> None:
        assessment = assessment_for(
            source=DeadLetterSource.OUTBOX,
            status="dead",
            reason=DeadLetterReason.NETWORK,
            attempts=3,
            remote_message_id="om_1234567890",
        )
        assert assessment.retryable
        assert not assessment.replay_safe
        assert assessment.requires_manual_review

    def test_manual_review_categories(self) -> None:
        for reason in (
            DeadLetterReason.AUTHENTICATION,
            DeadLetterReason.PERMISSION,
            DeadLetterReason.DATABASE,
            DeadLetterReason.UNKNOWN,
            DeadLetterReason.BUSINESS_CONFLICT,
        ):
            assessment = assessment_for(
                source=DeadLetterSource.EVENTS,
                status="dead",
                reason=reason,
                attempts=2,
            )
            assert assessment.requires_manual_review
            assert not assessment.replay_safe

    def test_non_dead_status_is_terminal(self) -> None:
        assessment = assessment_for(
            source=DeadLetterSource.OUTBOX,
            status="sent",
            reason=DeadLetterReason.NETWORK,
            attempts=1,
        )
        assert assessment.terminal
        assert not assessment.replay_safe


# ---------------------------------------------------------------------------
# Query + replay + resolve over the unified model
# ---------------------------------------------------------------------------


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    now = datetime.now(UTC)
    outbox_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            ReplyOutbox(
                id=outbox_id,
                event_id="evt-a",
                message_id="om_accept",
                reply_type="text",
                sequence=0,
                transport="feishu",
                payload_json={"text": "已记录支出 ¥100"},
                status=ReplyStatus.DEAD.value,
                attempt_count=1,
                last_error_code="HTTPStatusError",
                result_summary=(
                    "HTTPStatusError: Client error '400 Bad Request' for url "
                    "'https://open.feishu.cn/open-apis/im/v1/messages/om_accept/reply'"
                ),
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=1),
            )
        )
        session.add(
            ProcessedEvent(
                event_id="evt-timeout",
                payload_json={"payload_version": 1},
                payload_version=1,
                transport="webhook",
                status="failed",
                attempt_count=2,
                last_error_code="ReadTimeout",
                result_summary="ReadTimeout: request timed out",
                received_at=now - timedelta(days=3),
                processed_at=now - timedelta(days=3),
                updated_at=now - timedelta(hours=2),
            )
        )
        session.add(
            ProcessedEvent(
                event_id="evt-ok",
                payload_json={"payload_version": 1},
                payload_version=1,
                transport="websocket",
                status="succeeded",
                attempt_count=1,
                received_at=now - timedelta(days=4),
                processed_at=now - timedelta(days=4),
                updated_at=now - timedelta(days=4),
            )
        )
        await session.commit()
    return {"outbox": str(outbox_id)}


async def test_list_unified_and_filtered(factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(factory)
    query = DeadLetterQueryService(factory)
    page = await query.list_items()
    by_id = {item.id: item for item in page.items}
    assert "evt-timeout" in by_id
    assert by_id["evt-timeout"].reason_category == "timeout"
    assert by_id["evt-timeout"].retryable is True
    # outbox row classified remote_rejected / terminal
    outbox_items = [item for item in page.items if item.source == "outbox"]
    assert len(outbox_items) == 1
    assert outbox_items[0].reason_category == "remote_rejected"
    assert outbox_items[0].terminal is True
    assert outbox_items[0].replay_safe is False

    # source filter
    events_only = await query.list_items(source="events")
    assert all(item.source == "events" for item in events_only.items)
    # reason filter
    timeout_only = await query.list_items(reason="timeout")
    assert all(item.reason_category == "timeout" for item in timeout_only.items)
    # retryable filter
    retryable = await query.list_items(retryable=True)
    assert all(item.retryable for item in retryable.items)
    # pagination
    paged = await query.list_items(page=1, page_size=1)
    assert paged.total == 2
    assert len(paged.items) == 1


async def test_detail_redacted_with_audit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    query = DeadLetterQueryService(factory)
    detail = await query.detail("outbox", ids["outbox"])
    assert detail is not None
    assert detail.reason_category == "remote_rejected"
    assert detail.payload_summary == "reply/text"
    assert detail.message_id is not None
    # mask: message ids are shortened
    assert detail.message_id != "om_accept"
    assert "payload_json" not in detail.to_safe_dict()
    assert "text" not in (detail.last_error_summary or "")

    missing = await query.detail("outbox", str(uuid.uuid4()))
    assert missing is None


async def test_replay_requeues_and_duplicate_blocked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    ops = DeadLetterOpsService(factory)
    result = await ops.replay(
        "outbox", ids["outbox"], operator="ou_admin", reason="transient retry"
    )
    assert result.outcome == "requeued"
    assert result.before_status == "dead"
    assert result.after_status == "pending"

    # duplicate replay → conflict
    with pytest.raises(DeadLetterConflictError):
        await ops.replay("outbox", ids["outbox"], operator="ou_admin", reason="again")

    # worker can pick it up (state is pending now)
    async with factory() as session:
        row = await session.get(ReplyOutbox, uuid.UUID(ids["outbox"]))
        assert row is not None
        assert row.status == ReplyStatus.PENDING.value
        assert row.last_error_code is None
        assert row.result_summary is None


async def test_replay_not_found_and_unsupported_source(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    ops = DeadLetterOpsService(factory)
    with pytest.raises(DeadLetterNotFoundError):
        await ops.replay("outbox", str(uuid.uuid4()), operator="ou_admin", reason="x" * 3)
    with pytest.raises(ValueError):
        await ops.replay("pending_commands", str(uuid.uuid4()), operator="ou_admin", reason="x" * 3)


async def test_resolve_idempotent_and_audited(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    ops = DeadLetterOpsService(factory)
    first = await ops.resolve(
        "outbox", ids["outbox"], operator="ou_admin", reason="historical test fixture"
    )
    assert first.outcome == "resolved"
    second = await ops.resolve("outbox", ids["outbox"], operator="ou_admin", reason="duplicate")
    assert second.outcome == "already_resolved"
    assert second.audit_id == first.audit_id

    # source row untouched
    async with factory() as session:
        row = await session.get(ReplyOutbox, uuid.UUID(ids["outbox"]))
        assert row is not None
        assert row.status == ReplyStatus.DEAD.value


async def test_audit_rows_recorded_without_payload(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    ops = DeadLetterOpsService(factory)
    await ops.resolve("outbox", ids["outbox"], operator="ou_admin", reason="cleanup")
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(DeadLetterAction).where(DeadLetterAction.target_id == ids["outbox"])
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "resolve"
    assert row.operator == "ou_admin"
    assert row.reason == "cleanup"
    assert row.before_status == "dead"
    assert row.after_status == "dead"
    # no payload material is stored
    assert row.reason is not None
    assert "payload" not in {row.source, row.target_id, row.action, row.reason}


async def test_audit_request_id_correlated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    ops = DeadLetterOpsService(factory)
    await ops.replay(
        "outbox",
        ids["outbox"],
        operator="ou_admin",
        reason="correlated",
        request_id="req-abc-123",
    )
    async with factory() as session:
        row = await session.scalar(
            select(DeadLetterAction).where(DeadLetterAction.target_id == ids["outbox"])
        )
    assert row is not None
    assert row.request_id == "req-abc-123"


async def test_query_source_isolation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    query = DeadLetterQueryService(factory)
    # events list never contains outbox ids
    events = await query.list_items(source="events")
    assert ids["outbox"] not in [item.id for item in events.items]
    # detail of an outbox id under events source is None
    assert await query.detail("events", ids["outbox"]) is None


class TestClassificationExtra:
    def test_http_500_is_retryable(self) -> None:
        assert (
            classify_error_code("HTTPStatusError", "Server error '500 Internal Server Error'")
            is DeadLetterReason.NETWORK
        )

    def test_http_409_is_business_conflict(self) -> None:
        assert (
            classify_error_code("HTTPStatusError", "Client error '409 Conflict'")
            is DeadLetterReason.BUSINESS_CONFLICT
        )


async def _seed_pending(factory: async_sessionmaker[AsyncSession]) -> str:
    from lark_ledger.models import PendingCommand

    pending_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(
            PendingCommand(
                id=pending_id,
                confirmation_code="CAB123",
                user_open_id="ou_pending",
                status="expired",
                command_type="text",
                payload_json={"command_type": "text"},
                preview_json={},
                risk_reason="duplicate",
                expires_at=now - timedelta(hours=1),
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(hours=1),
            )
        )
        await session.commit()
    return str(pending_id)


async def test_pending_commands_query_and_resolve(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    pending_id = await _seed_pending(factory)
    query = DeadLetterQueryService(factory)
    page = await query.list_items(source="pending_commands", state="terminal")
    assert len(page.items) == 1
    item = page.items[0]
    assert item.state == "terminal"
    assert item.reason_category == "expired"
    assert item.terminal is True

    detail = await query.detail("pending_commands", pending_id)
    assert detail is not None
    assert detail.payload_summary == "command/text"

    # resolve on pending works (audit-only)
    ops = DeadLetterOpsService(factory)
    result = await ops.resolve(
        "pending_commands", pending_id, operator="ou_admin", reason="expired fixture"
    )
    assert result.outcome == "resolved"
    # replay on pending is unsupported
    with pytest.raises(ValueError):
        await ops.replay("pending_commands", pending_id, operator="ou_admin", reason="x" * 3)


async def test_events_replay_delegates_with_unified_audit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replaying an event goes through EventReplayService + unified audit row."""
    from lark_ledger.event_payload import (
        PAYLOAD_VERSION,
        REPLAY_SAFETY_VERSION,
        build_stored_payload,
    )

    now = datetime.now(UTC)
    event_id = "evt-replay-dead"
    payload = build_stored_payload(
        event_id,
        {
            "sender": {"sender_id": {"open_id": "ou_x"}},
            "message": {
                "message_id": f"om_{event_id}",
                "message_type": "text",
                "content": '{"text":"x"}',
            },
        },
        transport="webhook",
        received_at=now - timedelta(minutes=2),
    )
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id=event_id,
                payload_json=payload,
                payload_version=PAYLOAD_VERSION,
                replay_safety_version=REPLAY_SAFETY_VERSION,
                transport="webhook",
                status="dead",
                attempt_count=3,
                last_error_code="ReadTimeout",
                result_summary="ReadTimeout: timed out",
                source_message_id=f"om_{event_id}",
                received_at=now - timedelta(minutes=2),
                processed_at=now - timedelta(minutes=2),
            )
        )
        await session.commit()

    ops = DeadLetterOpsService(factory)
    result = await ops.replay(
        "events", event_id, operator="ou_admin", reason="dependency recovered"
    )
    assert result.outcome == "requeued"
    assert result.after_status == "received"

    async with factory() as session:
        row = await session.get(ProcessedEvent, event_id)
        assert row is not None
        assert row.status == "received"
        assert row.manual_replay_count == 1
        # unified audit row exists
        unified = (
            await session.execute(
                select(DeadLetterAction).where(
                    DeadLetterAction.source == "events",
                    DeadLetterAction.target_id == event_id,
                )
            )
        ).scalar_one_or_none()
        assert unified is not None
        assert unified.action == "replay"
        assert unified.before_status == "dead"
        assert unified.after_status == "received"

    # detail audit merges event_replay_audits + dead_letter_actions
    detail = await DeadLetterQueryService(factory).detail("events", event_id)
    assert detail is not None
    assert any(entry["action"] == "replay" for entry in detail.audit)
    assert len(detail.audit) >= 2


async def test_events_replay_rejected_by_preflight_conflicts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An event with committed business result cannot be blindly replayed."""
    from lark_ledger.event_payload import (
        PAYLOAD_VERSION,
        REPLAY_SAFETY_VERSION,
        build_stored_payload,
    )

    now = datetime.now(UTC)
    event_id = "evt-committed"
    payload = build_stored_payload(
        event_id,
        {
            "sender": {"sender_id": {"open_id": "ou_y"}},
            "message": {
                "message_id": f"om_{event_id}",
                "message_type": "text",
                "content": '{"text":"x"}',
            },
        },
        transport="webhook",
        received_at=now - timedelta(minutes=2),
    )
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id=event_id,
                payload_json=payload,
                payload_version=PAYLOAD_VERSION,
                replay_safety_version=REPLAY_SAFETY_VERSION,
                transport="webhook",
                status="dead",
                attempt_count=3,
                last_error_code="ReadTimeout",
                result_summary="ReadTimeout: timed out",
                source_message_id=f"om_{event_id}",
                business_committed_at=now - timedelta(minutes=1),
                received_at=now - timedelta(minutes=2),
                processed_at=now - timedelta(minutes=2),
            )
        )
        await session.commit()

    ops = DeadLetterOpsService(factory)
    with pytest.raises(DeadLetterConflictError):
        await ops.replay("events", event_id, operator="ou_admin", reason="blind replay attempt")


async def test_list_date_range_and_sort(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    query = DeadLetterQueryService(factory)
    now = datetime.now(UTC)
    # date-range filters
    recent = await query.list_items(created_from=now - timedelta(hours=1))
    assert recent.total == 0
    broad = await query.list_items(created_from=now - timedelta(days=4))
    assert broad.total == 2
    # status filter wins over state
    explicit = await query.list_items(status="dead")
    assert all(item.status == "dead" for item in explicit.items)
    # sort variants do not crash
    for sort in ("created_at", "attempts", "dead_at"):
        page = await query.list_items(sort=sort)
        assert page.total == 2
