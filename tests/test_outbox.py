"""P06a Transactional Outbox: envelope, status enum, and store primitives.

Uses SQLite in-memory for the single-session store behavior; the PostgreSQL
integration tests in ``tests/integration/test_outbox_postgres.py`` exercise the
same primitives against real storage.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.event_payload import MAX_RESULT_SUMMARY_LENGTH
from lark_ledger.models import Base, ProcessedEvent, ReplyOutbox
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_card_payload,
    build_file_payload,
    build_text_payload,
)
from lark_ledger.services.outbox import ReplyOutboxStore, record_failure_summary


@pytest_asyncio.fixture
async def factory() -> async_sessionmaker[Any]:
    """In-memory SQLite factory; the engine is disposed on teardown so no
    aiosqlite connection outlives the event loop."""
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


async def _insert_row(
    factory: async_sessionmaker[Any],
    *,
    event_id: str,
    message_id: str,
    reply_type: str = ReplyType.TEXT.value,
    status: str = ReplyStatus.PENDING.value,
) -> ReplyOutbox:
    async with factory() as session:
        row = ReplyOutbox(
            event_id=event_id,
            message_id=message_id,
            reply_type=reply_type,
            sequence=0,
            transport="feishu",
            payload_version=OUTBOX_PAYLOAD_VERSION,
            payload_json=build_text_payload("hello"),
            status=status,
        )
        session.add(row)
        await session.commit()
        return row


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def test_text_payload_embeds_final_text() -> None:
    payload = build_text_payload("已记录 #A83F2 支出 ¥32.00 · 餐饮")
    assert payload["payload_version"] == OUTBOX_PAYLOAD_VERSION
    assert payload["reply_type"] == ReplyType.TEXT.value
    assert payload["text"] == "已记录 #A83F2 支出 ¥32.00 · 餐饮"


def test_file_payload_records_metadata_and_checksum() -> None:
    content = b"short_id,amount\n#A83F2,32.00\n"
    payload = build_file_payload(
        filename="larkledger-export-v1-20260805.csv",
        content_type="text/csv",
        content=content,
    )
    assert payload["reply_type"] == ReplyType.FILE.value
    meta = payload["file"]
    assert meta["filename"] == "larkledger-export-v1-20260805.csv"
    assert meta["content_type"] == "text/csv"
    assert meta["size"] == len(content)
    import hashlib

    assert meta["sha256"] == hashlib.sha256(content).hexdigest()


def test_card_payload_with_and_without_image() -> None:
    plain = build_card_payload(card={"schema": "2.0", "body": {"elements": []}})
    assert plain["image"] is None
    with_image = build_card_payload(
        card={"schema": "2.0"},
        image_bytes=b"\x89PNG\r\n\x1a\nreport",
        image_alt="报告图表",
    )
    assert with_image["image"]["size"] == len(b"\x89PNG\r\n\x1a\nreport")
    assert with_image["image"]["alt"] == "报告图表"
    assert with_image["card"] == {"schema": "2.0"}


# ---------------------------------------------------------------------------
# Store primitives
# ---------------------------------------------------------------------------


async def test_has_outbox_reports_committed_rows(factory: async_sessionmaker[Any]) -> None:
    store = ReplyOutboxStore(factory)
    assert await store.has_outbox("evt_none") is False
    await _insert_row(factory, event_id="evt_yes", message_id="om_yes")
    assert await store.has_outbox("evt_yes") is True


async def test_load_by_ids_returns_committed_rows(factory: async_sessionmaker[Any]) -> None:
    row = await _insert_row(factory, event_id="evt_load", message_id="om_load")
    loaded = await store_load(factory, [row.id])
    assert len(loaded) == 1
    assert loaded[0].id == row.id
    assert loaded[0].payload_json["text"] == "hello"


async def store_load(factory: async_sessionmaker[Any], ids: list[Any]) -> list[ReplyOutbox]:
    return await ReplyOutboxStore(factory).load_by_ids(ids)


async def test_mark_sent_transitions_pending_to_sent(factory: async_sessionmaker[Any]) -> None:
    row = await _insert_row(factory, event_id="evt_sent", message_id="om_sent")
    store = ReplyOutboxStore(factory)
    assert await store.mark_sent(row.id, result_summary="delivered") is True

    async with factory() as session:
        reloaded = await session.get(ReplyOutbox, row.id)
    assert reloaded is not None
    assert reloaded.status == ReplyStatus.SENT.value
    assert reloaded.sent_at is not None
    assert reloaded.last_error_code is None


async def test_mark_sent_is_idempotent_for_sent_row(factory: async_sessionmaker[Any]) -> None:
    row = await _insert_row(
        factory, event_id="evt_sent2", message_id="om_sent2", status=ReplyStatus.SENT.value
    )
    store = ReplyOutboxStore(factory)
    # A sent row is never re-marked (and therefore never re-sent).
    assert await store.mark_sent(row.id, result_summary="again") is False
    assert await store.mark_failed(row.id, error_code="X", summary="nope") is False


async def test_mark_failed_records_redacted_summary_and_attempt(
    factory: async_sessionmaker[Any],
) -> None:
    row = await _insert_row(factory, event_id="evt_fail", message_id="om_fail")
    store = ReplyOutboxStore(factory)
    boom = RuntimeError("http://user:sekret@host/path and Bearer abc123XYZ")
    error_code, summary = record_failure_summary(boom)
    assert await store.mark_failed(row.id, error_code=error_code, summary=summary) is True

    async with factory() as session:
        reloaded = await session.get(ReplyOutbox, row.id)
    assert reloaded is not None
    assert reloaded.status == ReplyStatus.FAILED.value
    assert reloaded.attempt_count == 1
    assert reloaded.last_error_code == "RuntimeError"
    assert "sekret" not in (reloaded.result_summary or "")
    assert "abc123XYZ" not in (reloaded.result_summary or "")
    assert "http://user:***@host/path" in (reloaded.result_summary or "")


async def test_mark_failed_truncates_long_summary(factory: async_sessionmaker[Any]) -> None:
    row = await _insert_row(factory, event_id="evt_trunc", message_id="om_trunc")
    store = ReplyOutboxStore(factory)
    long_message = "x" * (MAX_RESULT_SUMMARY_LENGTH + 200)
    assert await store.mark_failed(row.id, error_code="E", summary=long_message) is True

    async with factory() as session:
        reloaded = await session.get(ReplyOutbox, row.id)
    assert reloaded is not None
    assert len(reloaded.result_summary or "") <= MAX_RESULT_SUMMARY_LENGTH


async def test_event_reply_type_unique_violation(factory: async_sessionmaker[Any]) -> None:
    await _insert_row(factory, event_id="evt_uniq", message_id="om_u", reply_type="text")
    async with factory() as session:
        session.add(
            ReplyOutbox(
                event_id="evt_uniq",
                message_id="om_u",
                reply_type=ReplyType.TEXT.value,
                sequence=1,
                transport="feishu",
                payload_version=OUTBOX_PAYLOAD_VERSION,
                payload_json=build_text_payload("duplicate"),
                status=ReplyStatus.PENDING.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_outbox_row_links_to_claimed_event(factory: async_sessionmaker[Any]) -> None:
    async with factory() as session:
        session.add(
            ProcessedEvent(
                event_id="evt_parent",
                status="received",
                payload_json={"payload_version": 1, "event_id": "evt_parent"},
            )
        )
        await session.commit()
    async with factory() as session:
        row = ReplyOutbox(
            event_id="evt_parent",
            message_id="om_parent",
            reply_type="text",
            sequence=0,
            transport="feishu",
            payload_version=1,
            payload_json=build_text_payload("hi"),
            status=ReplyStatus.PENDING.value,
        )
        session.add(row)
        await session.commit()
    async with factory() as session:
        # FK is defined; SQLite does not enforce it by default, but the column
        # stores the linkage for the recovery pre-check.
        rows = (await session.execute(select(ReplyOutbox))).scalars().all()
    assert rows[0].event_id == "evt_parent"
