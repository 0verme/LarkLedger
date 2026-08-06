"""P06b: reply delivery worker, lease, retry, dead, ordering, and sender.

SQLite in-memory mirrors the PostgreSQL claim / lease / retry state machine for
the single-connection case; PostgreSQL integration tests in
``tests/integration/test_reply_worker_postgres.py`` exercise the real
``SKIP LOCKED`` concurrency and lease semantics. All worker behavior here uses
injected clocks, sleepers, owner IDs, stores, deliverers, and a recording Feishu
client — no real time, no real sleep, no real network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.models import Base, ReplyOutbox
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyPayloadError,
    ReplyStatus,
    ReplyType,
    build_card_payload,
    build_file_payload,
    build_text_payload,
)
from lark_ledger.services.outbox import ReplyOutboxStore
from lark_ledger.services.reply_worker import (
    WORKER_TASK_NAME,
    ReplyDeliverer,
    ReplyWorker,
    card_with_image,
    is_permanent_reply_error,
)

T0 = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


def _naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _file_bytes() -> bytes:
    return b"short_id,amount\n#A83F2,32.00\n"


class RecordingFeishu:
    """Records every delivery call including the Feishu ``uuid`` idempotency key."""

    def __init__(
        self,
        *,
        reply_error: BaseException | None = None,
        upload_error: BaseException | None = None,
        reply_file_error: BaseException | None = None,
        reply_card_error: BaseException | None = None,
    ) -> None:
        self.reply_error = reply_error
        self.upload_error = upload_error
        self.reply_file_error = reply_file_error
        self.reply_card_error = reply_card_error
        self.text_calls: list[tuple[str, str, str | None]] = []
        self.file_calls: list[tuple[str, str, str | None]] = []
        self.card_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.uploads: list[tuple[bytes, str]] = []
        self.image_uploads: list[bytes] = []

    async def reply_text(self, message_id: str, text: str, *, uuid: str | None = None) -> None:
        if self.reply_error is not None:
            raise self.reply_error
        self.text_calls.append((message_id, text, uuid))

    async def reply_file(
        self, message_id: str, file_key: str, *, uuid: str | None = None
    ) -> None:
        if self.reply_file_error is not None:
            raise self.reply_file_error
        self.file_calls.append((message_id, file_key, uuid))

    async def reply_card(
        self, message_id: str, card: dict[str, Any], *, uuid: str | None = None
    ) -> None:
        if self.reply_card_error is not None:
            raise self.reply_card_error
        self.card_calls.append((message_id, card, uuid))

    async def upload_file(self, content: bytes, filename: str) -> str:
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append((content, filename))
        return "file_key_1"

    async def upload_image(self, png: bytes) -> str:
        if self.upload_error is not None:
            raise self.upload_error
        self.image_uploads.append(png)
        return "image_key_1"


async def _insert(
    factory: async_sessionmaker[Any],
    *,
    event_id: str | None = "evt_1",
    message_id: str = "om_1",
    reply_type: str = ReplyType.TEXT.value,
    sequence: int = 0,
    payload: dict[str, Any] | None = None,
    blob: bytes | None = None,
    status: str = ReplyStatus.PENDING.value,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    remote_file_key: str | None = None,
    remote_image_key: str | None = None,
) -> ReplyOutbox:
    if payload is None:
        payload = build_text_payload("hello")
    async with factory() as session:
        row = ReplyOutbox(
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
            remote_file_key=remote_file_key,
            remote_image_key=remote_image_key,
        )
        session.add(row)
        await session.commit()
        return row


async def _row(factory: async_sessionmaker[Any], outbox_id: Any) -> ReplyOutbox:
    async with factory() as session:
        row = await session.get(ReplyOutbox, outbox_id)
        assert row is not None
        return row


def _store(factory: async_sessionmaker[Any]) -> ReplyOutboxStore:
    return ReplyOutboxStore(factory)


def _deliverer(
    store: ReplyOutboxStore,
    feishu: RecordingFeishu,
    *,
    owner_id: str = "w1",
    **kwargs: Any,
) -> ReplyDeliverer:
    return ReplyDeliverer(store, feishu, owner_id=owner_id, jitter=None, **kwargs)


def _worker(
    store: ReplyOutboxStore,
    feishu: RecordingFeishu,
    *,
    owner_id: str = "w1",
    retry_base_seconds: float = 2.0,
    retry_max_seconds: float = 3600.0,
    max_attempts: int = 3,
    **kwargs: Any,
) -> ReplyWorker:
    return ReplyWorker(
        store,
        _deliverer(
            store,
            feishu,
            owner_id=owner_id,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            max_attempts=max_attempts,
        ),
        owner_id=owner_id,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Pure error classification
# ---------------------------------------------------------------------------


def test_permanent_reply_error_classification() -> None:
    assert is_permanent_reply_error(ReplyPayloadError("bad payload")) is True
    assert is_permanent_reply_error(ValueError("contract")) is True
    assert is_permanent_reply_error(TypeError("type")) is True
    assert is_permanent_reply_error(RuntimeError("transient network")) is False
    assert is_permanent_reply_error(ConnectionError("down")) is False
    assert is_permanent_reply_error(TimeoutError("slow")) is False


def test_permanent_reply_error_http_status_classification() -> None:
    import httpx

    request = httpx.Request("POST", "https://open.feishu.cn/x")
    for code in (400, 401, 403, 404, 422):
        err = httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(code, request=request)
        )
        assert is_permanent_reply_error(err) is True, f"{code} should be permanent"
    for code in (408, 429, 500, 502, 503):
        err = httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(code, request=request)
        )
        assert is_permanent_reply_error(err) is False, f"{code} should be retryable"


# ---------------------------------------------------------------------------
# Store: claim conditions and lease
# ---------------------------------------------------------------------------


async def test_claim_picks_pending_and_writes_lease_and_attempt() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_c", message_id="om_c")
    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in claimed] == [row.id]
    assert claimed[0].attempt_count == 1

    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.SENDING.value
    assert reloaded.lease_owner == "w1"
    assert reloaded.lease_expires_at == _naive(T0 + timedelta(seconds=300))
    assert reloaded.next_attempt_at is None
    await engine.dispose()


async def test_claim_condition_matrix() -> None:
    engine, factory = await _sqlite_factory()
    future = T0 + timedelta(hours=1)
    past = T0 - timedelta(hours=1)
    pending_row = await _insert(factory, event_id="pending", message_id="om_a")
    failed_due = await _insert(
        factory,
        event_id="failed_due",
        message_id="om_b",
        status=ReplyStatus.FAILED.value,
        attempt_count=1,
        next_attempt_at=past,
    )
    await _insert(
        factory,
        event_id="failed_future",
        message_id="om_c",
        status=ReplyStatus.FAILED.value,
        attempt_count=1,
        next_attempt_at=future,
    )
    await _insert(
        factory,
        event_id="sending_active",
        message_id="om_d",
        status=ReplyStatus.SENDING.value,
        lease_owner="other",
        lease_expires_at=future,
    )
    sending_expired = await _insert(
        factory,
        event_id="sending_expired",
        message_id="om_e",
        status=ReplyStatus.SENDING.value,
        attempt_count=1,
        lease_owner="old",
        lease_expires_at=past,
    )
    await _insert(
        factory, event_id="sent", message_id="om_f", status=ReplyStatus.SENT.value
    )
    await _insert(
        factory, event_id="dead", message_id="om_g", status=ReplyStatus.DEAD.value
    )

    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    ids = {item.id for item in claimed}
    attempts = {item.id: item.attempt_count for item in claimed}

    assert pending_row.id in ids
    assert failed_due.id in ids
    assert attempts[failed_due.id] == 2  # a second claim after a P06a failure
    assert attempts[pending_row.id] == 1
    # sending with a valid lease, failed-but-not-due, sent, dead are excluded.
    assert all(
        item.event_id not in {"failed_future", "sending_active", "sent", "dead"}
        for item in claimed
    )
    assert sending_expired.id in ids  # reclaim of an expired lease
    assert attempts[sending_expired.id] == 2
    await engine.dispose()


async def test_claim_is_idempotent_within_one_sweep() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_once", message_id="om_once")
    store = _store(factory)
    first = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    second = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in first] == [row.id]
    assert second == []
    await engine.dispose()


async def test_claim_by_id_claims_only_the_given_row() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_a", message_id="om_a")
    other = await _insert(factory, event_id="evt_b", message_id="om_b")
    store = _store(factory)
    item = await store.claim_by_id(other.id, "w1", T0, lease_seconds=300.0)
    assert item is not None and item.id == other.id
    # The first row is still pending (claim_by_id does not touch it).
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.PENDING.value
    await engine.dispose()


async def test_claim_by_id_returns_none_for_terminal_row() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(
        factory, event_id="evt_sent", message_id="om_sent", status=ReplyStatus.SENT.value
    )
    assert await _store(factory).claim_by_id(row.id, "w1", T0, lease_seconds=300.0) is None
    await engine.dispose()


async def test_lease_guards_sent_and_failure() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_lease", message_id="om_lease")
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None

    # A non-owner cannot mark sent or record a failure.
    assert await store.mark_sent(row.id, "w2", now=T0) is False
    assert (
        await store.record_failure(
            row.id, "w2", status=ReplyStatus.FAILED.value, next_attempt_at=None,
            error_code="X", summary="nope", now=T0,
        )
        is False
    )
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.SENDING.value
    assert reloaded.lease_owner == "w1"

    # The current owner can mark sent; the lease is cleared and the remote id saved.
    assert await store.mark_sent(row.id, "w1", now=T0, remote_message_id="om_reply") is True
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.SENT.value
    assert reloaded.sent_at == _naive(T0)
    assert reloaded.lease_owner is None
    assert reloaded.lease_expires_at is None
    assert reloaded.remote_message_id == "om_reply"
    await engine.dispose()


async def test_expired_lease_reclaim_and_stale_worker_cannot_overwrite() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_reclaim", message_id="om_reclaim")
    store = _store(factory)
    await store.claim_by_id(row.id, "worker-a", T0, lease_seconds=300.0)

    later = T0 + timedelta(seconds=301)
    reclaimed = await store.claim_batch("worker-b", later, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in reclaimed] == [row.id]
    assert reclaimed[0].attempt_count == 2

    # The stale worker cannot overwrite the new owner's state.
    assert await store.mark_sent(row.id, "worker-a", now=later) is False
    assert (
        await store.record_failure(
            row.id, "worker-a", status=ReplyStatus.DEAD.value, next_attempt_at=None,
            error_code="X", summary="stale", now=later,
        )
        is False
    )
    await engine.dispose()


async def test_remote_key_persistence_is_lease_guarded() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_key", message_id="om_key")
    store = _store(factory)
    await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert await store.persist_file_key(row.id, "w1", file_key="fk_1", now=T0) is True
    assert await store.persist_image_key(row.id, "w1", image_key="ik_1", now=T0) is True
    # A non-owner cannot persist a key.
    assert await store.persist_file_key(row.id, "w2", file_key="fk_2", now=T0) is False

    reloaded = await _row(factory, row.id)
    assert reloaded.remote_file_key == "fk_1"
    assert reloaded.remote_image_key == "ik_1"
    # The row is still sending (the lease is retained while keys are persisted).
    assert reloaded.status == ReplyStatus.SENDING.value
    assert reloaded.lease_owner == "w1"
    await engine.dispose()


# ---------------------------------------------------------------------------
# Ordering: one event's replies never overtake each other
# ---------------------------------------------------------------------------


async def _file_text_rows(
    factory: async_sessionmaker[Any], event_id: str
) -> tuple[ReplyOutbox, ReplyOutbox]:
    """Create the real CSV shape: file seq 0 then confirmation text seq 1."""
    file_row = await _insert(
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
    text_row = await _insert(
        factory,
        event_id=event_id,
        message_id="om_order",
        reply_type=ReplyType.TEXT.value,
        sequence=1,
        payload=build_text_payload("已导出 1 笔"),
    )
    return file_row, text_row


async def test_later_reply_waits_for_earlier_pending() -> None:
    engine, factory = await _sqlite_factory()
    file_row, _text_row = await _file_text_rows(factory, "evt_order")
    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in claimed] == [file_row.id]  # seq 0 first, never seq 1
    await engine.dispose()


async def test_later_reply_is_claimable_after_earlier_sent() -> None:
    engine, factory = await _sqlite_factory()
    file_row, text_row = await _file_text_rows(factory, "evt_order2")
    store = _store(factory)
    first = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in first] == [file_row.id]
    await store.mark_sent(file_row.id, "w1", now=T0)

    second = await store.claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in second] == [text_row.id]
    await engine.dispose()


async def test_failed_earlier_reply_blocks_later_one() -> None:
    engine, factory = await _sqlite_factory()
    file_row, _text_row = await _file_text_rows(factory, "evt_order3")
    async with factory() as session:
        row = await session.get(ReplyOutbox, file_row.id)
        assert row is not None
        row.status = ReplyStatus.FAILED.value
        row.next_attempt_at = T0 + timedelta(hours=1)  # not yet due
        await session.commit()
    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert claimed == []  # seq 1 waits while seq 0 is retrying
    await engine.dispose()


async def test_dead_earlier_reply_allows_later_one() -> None:
    engine, factory = await _sqlite_factory()
    file_row, text_row = await _file_text_rows(factory, "evt_order4")
    async with factory() as session:
        row = await session.get(ReplyOutbox, file_row.id)
        assert row is not None
        row.status = ReplyStatus.DEAD.value
        await session.commit()
    claimed = await _store(factory).claim_batch("w1", T0, batch_size=10, lease_seconds=300.0)
    assert [item.id for item in claimed] == [text_row.id]
    await engine.dispose()


# ---------------------------------------------------------------------------
# Sender: text / file / card from persisted payloads only
# ---------------------------------------------------------------------------


async def test_text_delivery_sends_persisted_text_with_idempotency_uuid() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(
        factory,
        event_id="evt_text",
        message_id="om_text",
        payload=build_text_payload("已记录 #A83F2 支出 ¥32.00 · 餐饮"),
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu()
    outcome = await _deliverer(store, feishu).process_item(item, T0)

    assert outcome == ReplyStatus.SENT.value
    assert feishu.text_calls == [
        ("om_text", "已记录 #A83F2 支出 ¥32.00 · 餐饮", row.id.hex)
    ]
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.SENT.value
    await engine.dispose()


async def test_file_delivery_uploads_from_blob_and_persists_file_key() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(
        factory,
        event_id="evt_file",
        message_id="om_file",
        reply_type=ReplyType.FILE.value,
        payload=build_file_payload(
            filename="larkledger-export-v1.csv",
            content_type="text/csv",
            content=_file_bytes(),
        ),
        blob=_file_bytes(),
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu()
    outcome = await _deliverer(store, feishu).process_item(item, T0)

    assert outcome == ReplyStatus.SENT.value
    assert feishu.uploads == [(_file_bytes(), "larkledger-export-v1.csv")]
    assert feishu.file_calls == [("om_file", "file_key_1", row.id.hex)]
    reloaded = await _row(factory, row.id)
    assert reloaded.remote_file_key == "file_key_1"
    await engine.dispose()


async def test_file_retry_reuses_persisted_key_without_reuploading() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(
        factory,
        event_id="evt_file_retry",
        message_id="om_file_retry",
        reply_type=ReplyType.FILE.value,
        payload=build_file_payload(
            filename="larkledger-export-v1.csv",
            content_type="text/csv",
            content=_file_bytes(),
        ),
        blob=_file_bytes(),
        remote_file_key="file_key_kept",
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None and item.remote_file_key == "file_key_kept"
    feishu = RecordingFeishu(reply_file_error=RuntimeError("send boom"))
    deliverer = _deliverer(store, feishu)
    outcome = await deliverer.process_item(item, T0)
    assert outcome == ReplyStatus.FAILED.value

    # Retry after the backoff window: the persisted key is reused, no re-upload.
    later = T0 + timedelta(seconds=3)
    item2 = await store.claim_by_id(row.id, "w1", later, lease_seconds=300.0)
    assert item2 is not None
    feishu.reply_file_error = None
    outcome2 = await deliverer.process_item(item2, T0)
    assert outcome2 == ReplyStatus.SENT.value
    assert feishu.uploads == []  # never re-uploaded
    # Only the successful retry is recorded (the failing attempt raised first);
    # it reused the persisted key instead of uploading again.
    assert feishu.file_calls == [("om_file_retry", "file_key_kept", row.id.hex)]
    await engine.dispose()


async def test_card_delivery_persists_image_key_and_injects_image() -> None:
    engine, factory = await _sqlite_factory()
    png = b"\x89PNG\r\n\x1a\nreport"
    row = await _insert(
        factory,
        event_id="evt_card",
        message_id="om_card",
        reply_type=ReplyType.CARD.value,
        payload=build_card_payload(
            card={"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "报告"}]}},
            image_bytes=png,
            image_alt="消费报告图表",
        ),
        blob=png,
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu()
    outcome = await _deliverer(store, feishu).process_item(item, T0)

    assert outcome == ReplyStatus.SENT.value
    assert feishu.image_uploads == [png]
    assert len(feishu.card_calls) == 1
    sent_card = feishu.card_calls[0][1]
    assert any(el.get("tag") == "img" and el.get("img_key") == "image_key_1"
               for el in sent_card["body"]["elements"])
    reloaded = await _row(factory, row.id)
    assert reloaded.remote_image_key == "image_key_1"
    await engine.dispose()


async def test_card_send_retry_reuses_image_key_without_reuploading() -> None:
    engine, factory = await _sqlite_factory()
    png = b"\x89PNG\r\n\x1a\nreport"
    row = await _insert(
        factory,
        event_id="evt_card_retry",
        message_id="om_card_retry",
        reply_type=ReplyType.CARD.value,
        payload=build_card_payload(
            card={"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "报告"}]}},
            image_bytes=png,
            image_alt="报告图表",
        ),
        blob=png,
        remote_image_key="image_key_kept",
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu(reply_card_error=RuntimeError("card boom"))
    deliverer = _deliverer(store, feishu)
    assert await deliverer.process_item(item, T0) == ReplyStatus.FAILED.value
    assert feishu.image_uploads == []  # key was already persisted; no re-upload

    later = T0 + timedelta(seconds=3)
    feishu.reply_card_error = None
    item2 = await store.claim_by_id(row.id, "w1", later, lease_seconds=300.0)
    assert item2 is not None
    assert await deliverer.process_item(item2, T0) == ReplyStatus.SENT.value
    assert feishu.image_uploads == []
    await engine.dispose()


async def test_temporary_http_500_marks_failed_then_retries_to_sent() -> None:
    """A genuine Feishu HTTP 500 is a retryable delivery failure.

    The first attempt records ``failed`` on the outbox row with backoff (never
    the event, never business); a retry after the backoff window succeeds and
    the same row is marked ``sent`` with the stable Feishu ``uuid`` key.
    """
    import httpx

    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_http500", message_id="om_http500")
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None

    request = httpx.Request("POST", "https://open.feishu.cn/open-apis/reply")
    feishu = RecordingFeishu(
        reply_error=httpx.HTTPStatusError(
            "temporary server error",
            request=request,
            response=httpx.Response(500, request=request),
        )
    )
    deliverer = _deliverer(store, feishu)
    assert await deliverer.process_item(item, T0) == ReplyStatus.FAILED.value
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.FAILED.value
    assert reloaded.last_error_code == "HTTPStatusError"
    assert reloaded.next_attempt_at == _naive(T0 + timedelta(seconds=2))  # backoff

    # After the backoff window a healthy retry succeeds on the same row.
    later = T0 + timedelta(seconds=3)
    feishu.reply_error = None
    item2 = await store.claim_by_id(row.id, "w1", later, lease_seconds=300.0)
    assert item2 is not None
    assert await deliverer.process_item(item2, T0) == ReplyStatus.SENT.value
    reloaded2 = await _row(factory, row.id)
    assert reloaded2.status == ReplyStatus.SENT.value
    assert reloaded2.sent_at is not None
    assert reloaded2.lease_owner is None
    # The Feishu uuid idempotency key stayed stable across both attempts.
    assert feishu.text_calls == [("om_http500", "hello", row.id.hex)]
    await engine.dispose()


async def test_card_image_upload_failure_degrades_to_text_only_card() -> None:
    engine, factory = await _sqlite_factory()
    png = b"\x89PNG\r\n\x1a\nreport"
    row = await _insert(
        factory,
        event_id="evt_card_degrade",
        message_id="om_card_degrade",
        reply_type=ReplyType.CARD.value,
        payload=build_card_payload(
            card={"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "报告"}]}},
            image_bytes=png,
            image_alt="报告图表",
        ),
        blob=png,
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu(upload_error=RuntimeError("upload unavailable"))
    outcome = await _deliverer(store, feishu).process_item(item, T0)

    # The stored text-only card is delivered; the row is not marked failed.
    assert outcome == ReplyStatus.SENT.value
    assert len(feishu.card_calls) == 1
    sent_card = feishu.card_calls[0][1]
    assert not any(el.get("tag") == "img" for el in sent_card["body"]["elements"])
    await engine.dispose()


async def test_checksum_mismatch_moves_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(
        factory,
        event_id="evt_checksum",
        message_id="om_checksum",
        reply_type=ReplyType.FILE.value,
        payload=build_file_payload(
            filename="larkledger-export-v1.csv",
            content_type="text/csv",
            content=_file_bytes(),
        ),
        blob=b"different bytes than recorded",
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu()
    outcome = await _deliverer(store, feishu).process_item(item, T0)

    assert outcome == ReplyStatus.DEAD.value
    assert feishu.uploads == []
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.DEAD.value
    assert reloaded.last_error_code == "ReplyPayloadError"
    assert reloaded.lease_owner is None
    await engine.dispose()


async def test_unsupported_version_and_type_move_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(
        factory,
        event_id="evt_version",
        message_id="om_version",
        payload=build_text_payload("hello"),
    )
    async with factory() as session:
        await session.execute(
            ReplyOutbox.__table__.update()
            .where(ReplyOutbox.id == row.id)
            .values(payload_version=999)
        )
        await session.commit()
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    outcome = await _deliverer(store, RecordingFeishu()).process_item(item, T0)
    assert outcome == ReplyStatus.DEAD.value

    unknown = await _insert(
        factory,
        event_id="evt_unknown_type",
        message_id="om_unknown",
        reply_type="video",
        payload=build_text_payload("hello"),
    )
    item2 = await store.claim_by_id(unknown.id, "w1", T0, lease_seconds=300.0)
    assert item2 is not None
    outcome2 = await _deliverer(store, RecordingFeishu()).process_item(item2, T0)
    assert outcome2 == ReplyStatus.DEAD.value
    await engine.dispose()


async def test_payload_integrity_violations_move_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    # Missing payload_blob for a file row that requires it.
    row = await _insert(
        factory,
        event_id="evt_missing_blob",
        message_id="om_missing_blob",
        reply_type=ReplyType.FILE.value,
        payload=build_file_payload(
            filename="larkledger-export-v1.csv",
            content_type="text/csv",
            content=_file_bytes(),
        ),
        blob=None,
    )
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    outcome = await _deliverer(store, RecordingFeishu()).process_item(item, T0)
    assert outcome == ReplyStatus.DEAD.value

    # A card whose envelope promises an image but whose blob is gone is also a
    # permanent contract violation (dead), not a silent text-only degrade.
    card_row = await _insert(
        factory,
        event_id="evt_missing_card_blob",
        message_id="om_missing_card_blob",
        reply_type=ReplyType.CARD.value,
        payload=build_card_payload(
            card={"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "报告"}]}},
            image_bytes=b"\x89PNG\r\n\x1a\nreport",
            image_alt="报告图表",
        ),
        blob=None,
    )
    card_item = await store.claim_by_id(card_row.id, "w1", T0, lease_seconds=300.0)
    assert card_item is not None
    card_outcome = await _deliverer(store, RecordingFeishu()).process_item(card_item, T0)
    assert card_outcome == ReplyStatus.DEAD.value
    await engine.dispose()


async def test_transient_error_records_failed_with_backoff() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_retry", message_id="om_retry")
    store = _store(factory)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert item is not None
    feishu = RecordingFeishu(reply_error=RuntimeError("feishu timeout"))
    outcome = await _deliverer(
        store, feishu, retry_base_seconds=2.0, retry_max_seconds=3600.0
    ).process_item(item, T0)

    assert outcome == ReplyStatus.FAILED.value
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.FAILED.value
    assert reloaded.attempt_count == 1
    assert reloaded.next_attempt_at == _naive(T0 + timedelta(seconds=2))
    assert reloaded.lease_owner is None
    assert reloaded.last_error_code == "RuntimeError"
    assert reloaded.result_summary == "RuntimeError: feishu timeout"
    await engine.dispose()


async def test_attempt_budget_exhausted_moves_to_dead() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_exhaust", message_id="om_exhaust")
    store = _store(factory)
    deliverer = _deliverer(
        store,
        RecordingFeishu(reply_error=RuntimeError("always fails")),
        max_attempts=2,
        retry_base_seconds=2.0,
        retry_max_seconds=3600.0,
    )
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    assert await deliverer.process_item(item, T0) == ReplyStatus.FAILED.value

    later = T0 + timedelta(seconds=3)
    item2 = await store.claim_by_id(row.id, "w1", later, lease_seconds=300.0)
    assert item2 is not None and item2.attempt_count == 2
    assert await deliverer.process_item(item2, later) == ReplyStatus.DEAD.value

    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.DEAD.value
    assert reloaded.next_attempt_at is None
    await engine.dispose()


# ---------------------------------------------------------------------------
# Worker orchestration
# ---------------------------------------------------------------------------


async def test_worker_claims_and_delivers_pending_reply() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_ok", message_id="om_ok")
    feishu = RecordingFeishu()
    worker = _worker(_store(factory), feishu, owner_id="w1")
    count = await worker.run_once(now=T0)
    assert count == 1
    assert len(feishu.text_calls) == 1
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.SENT.value
    await engine.dispose()


async def test_single_reply_failure_does_not_kill_worker_sweep() -> None:
    engine, factory = await _sqlite_factory()
    row_a = await _insert(factory, event_id="evt_a", message_id="om_a")
    row_b = await _insert(factory, event_id="evt_b", message_id="om_b")
    store = _store(factory)

    class FailOnceFeishu(RecordingFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def reply_text(
            self, message_id: str, text: str, *, uuid: str | None = None
        ) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("transient")
            self.text_calls.append((message_id, text, uuid))

    feishu = FailOnceFeishu()
    worker = _worker(store, feishu, owner_id="w1", retry_base_seconds=2.0)
    count = await worker.run_once(now=T0)  # must not raise
    assert count == 2
    a = await _row(factory, row_a.id)
    b = await _row(factory, row_b.id)
    # One delivery fails (recorded, retryable); the other is delivered. The
    # sweep survives the single failure regardless of claim order.
    statuses = {a.status, b.status}
    assert statuses == {ReplyStatus.FAILED.value, ReplyStatus.SENT.value}
    await engine.dispose()


async def test_worker_start_stop_leaves_no_dangling_task() -> None:
    engine, factory = await _sqlite_factory()
    worker = _worker(
        _store(factory),
        RecordingFeishu(),
        owner_id="w1",
        sleeper=lambda _delay: asyncio.sleep(3600),
    )
    try:
        worker.start()
        assert worker.running is True
        await asyncio.sleep(0)
    finally:
        await worker.stop()
    assert worker.running is False
    pending = [
        task for task in asyncio.all_tasks() if task.get_name() == WORKER_TASK_NAME
    ]
    assert pending == []
    await engine.dispose()


async def test_wakeup_sets_event_and_polling_is_source_of_truth() -> None:
    engine, factory = await _sqlite_factory()
    wakeup = asyncio.Event()
    worker = _worker(
        _store(factory),
        RecordingFeishu(),
        owner_id="w1",
        sleeper=lambda _delay: asyncio.sleep(3600),
        wakeup_event=wakeup,
    )
    assert wakeup.is_set() is False
    worker.wakeup()
    assert wakeup.is_set() is True

    # Even without a wakeup signal, a DB poll finds the row.
    row = await _insert(factory, event_id="evt_poll", message_id="om_poll")
    count = await worker.run_once(now=T0)
    assert count == 1
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.SENT.value
    await engine.dispose()


async def test_worker_stop_prevents_new_claims() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_stop", message_id="om_stop")
    worker = _worker(_store(factory), RecordingFeishu(), owner_id="w1")
    worker._stop.set()
    assert await worker.run_once(now=T0) == 0
    reloaded = await _row(factory, row.id)
    assert reloaded.status == ReplyStatus.PENDING.value
    await engine.dispose()


# ---------------------------------------------------------------------------
# Idempotency: uuid key and remote message id capture
# ---------------------------------------------------------------------------


async def test_send_uses_stable_outbox_id_as_feishu_uuid() -> None:
    engine, factory = await _sqlite_factory()
    row = await _insert(factory, event_id="evt_uuid", message_id="om_uuid")
    store = _store(factory)
    feishu = RecordingFeishu()
    deliverer = _deliverer(store, feishu)
    item = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    await deliverer.process_item(item, T0)
    # The same deterministic key is used across attempts (dedup by Feishu).
    assert feishu.text_calls[0][2] == row.id.hex

    item2 = await store.claim_by_id(row.id, "w1", T0, lease_seconds=300.0)
    # already sent: not claimable again
    assert item2 is None
    await engine.dispose()


async def test_card_with_image_helper() -> None:
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": "消息"},
                {"tag": "markdown", "content": "建议"},
            ]
        },
    }
    rebuilt = card_with_image(card, "img_x", "消费图表")
    assert rebuilt["body"]["elements"][0]["content"] == "消息"
    img = rebuilt["body"]["elements"][1]
    assert img["tag"] == "img"
    assert img["img_key"] == "img_x"
    assert img["alt"] == {"tag": "plain_text", "content": "消费图表"}
