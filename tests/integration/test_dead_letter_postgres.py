"""P44 PostgreSQL tests: locked replay / resolve, worker pickup, audit, migration.

Real-PostgreSQL coverage for the concurrency contract that SQLite cannot
prove: two operators replaying the same dead-letter at once must produce
exactly one state transition; after replay the reply worker must be able to
claim the row; resolve stays idempotent; audit rows are written; and the
migration round-trips cleanly on a scratch database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from lark_ledger.models import DeadLetterAction, ReplyOutbox
from lark_ledger.outbox import ReplyStatus
from lark_ledger.services.dead_letter import (
    DeadLetterConflictError,
    DeadLetterOpsService,
    DeadLetterQueryService,
)
from lark_ledger.services.outbox import ReplyOutboxStore

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _dead_outbox(*, event_id: str | None = None) -> ReplyOutbox:
    return ReplyOutbox(
        event_id=event_id,
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
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(days=1),
    )


async def test_concurrent_replay_produces_exactly_one_transition(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox = _dead_outbox()
    async with postgres_session_factory() as session:
        session.add(outbox)
        await session.commit()
    outbox_id = str(outbox.id)

    service = DeadLetterOpsService(postgres_session_factory)

    async def _attempt(operator: str) -> str:
        try:
            result = await service.replay(
                "outbox", outbox_id, operator=operator, reason="dependency recovered"
            )
            return result.outcome
        except DeadLetterConflictError:
            return "conflict"

    first, second = await asyncio.gather(_attempt("operator-a"), _attempt("operator-b"))
    outcomes = sorted([first, second])
    assert outcomes == ["conflict", "requeued"]

    async with postgres_session_factory() as session:
        row = await session.get(ReplyOutbox, outbox.id)
        assert row is not None
        assert row.status == ReplyStatus.PENDING.value
        assert row.last_error_code is None
        assert row.result_summary is None
        audits = int(await session.scalar(select(func.count()).select_from(DeadLetterAction)) or 0)
        # exactly one replay audit (the losing request wrote none)
        assert audits == 1
        audit = await session.scalar(select(DeadLetterAction))
        assert audit is not None
        assert audit.action == "replay"
        assert audit.before_status == "dead"
        assert audit.after_status == "pending"


async def test_replayed_outbox_is_claimed_by_worker(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox = _dead_outbox()
    async with postgres_session_factory() as session:
        session.add(outbox)
        await session.commit()

    await DeadLetterOpsService(postgres_session_factory).replay(
        "outbox", str(outbox.id), operator="operator", reason="dependency recovered"
    )

    store = ReplyOutboxStore(postgres_session_factory)
    claimed = await store.claim_batch(
        owner_id="worker-1", now=NOW + timedelta(seconds=1), batch_size=5, lease_seconds=60
    )
    assert any(claimed_row.id == outbox.id for claimed_row in claimed)
    async with postgres_session_factory() as session:
        row = await session.get(ReplyOutbox, outbox.id)
        assert row is not None
        assert row.status == ReplyStatus.SENDING.value
        assert row.lease_owner == "worker-1"
        assert row.attempt_count == 2  # the replay reset attempts, worker counts the fresh try


async def test_resolve_idempotent_on_real_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox = _dead_outbox()
    async with postgres_session_factory() as session:
        session.add(outbox)
        await session.commit()

    service = DeadLetterOpsService(postgres_session_factory)
    first = await service.resolve(
        "outbox", str(outbox.id), operator="operator", reason="terminal test fixture"
    )
    second = await service.resolve(
        "outbox", str(outbox.id), operator="operator", reason="duplicate"
    )
    assert first.outcome == "resolved"
    assert second.outcome == "already_resolved"
    assert second.audit_id == first.audit_id

    async with postgres_session_factory() as session:
        count = int(await session.scalar(select(func.count()).select_from(DeadLetterAction)) or 0)
        row = await session.get(ReplyOutbox, outbox.id)
    assert count == 1
    assert row is not None
    assert row.status == ReplyStatus.DEAD.value  # resolve never rewrites source rows

    # the unified query marks it resolved
    detail = await DeadLetterQueryService(postgres_session_factory).detail("outbox", str(outbox.id))
    assert detail is not None
    assert detail.resolved is True
    assert len(detail.audit) == 1


async def test_concurrent_resolve_writes_exactly_one_audit(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two operators resolving the same dead-letter concurrently must produce
    exactly one resolve audit; the losing request reports already_resolved.
    """
    outbox = _dead_outbox()
    async with postgres_session_factory() as session:
        session.add(outbox)
        await session.commit()
    outbox_id = str(outbox.id)

    service = DeadLetterOpsService(postgres_session_factory)

    async def _attempt(operator: str) -> str:
        result = await service.resolve(
            "outbox", outbox_id, operator=operator, reason="concurrent resolve"
        )
        return result.outcome

    first, second = await asyncio.gather(_attempt("operator-a"), _attempt("operator-b"))
    assert sorted([first, second]) == ["already_resolved", "resolved"]

    async with postgres_session_factory() as session:
        audits = (
            await session.scalars(
                select(DeadLetterAction).where(DeadLetterAction.target_id == outbox_id)
            )
        ).all()
        row = await session.get(ReplyOutbox, outbox.id)
    assert len(audits) == 1
    assert audits[0].action == "resolve"
    assert row is not None
    assert row.status == ReplyStatus.DEAD.value  # resolve never rewrites source rows


async def test_replay_non_dead_row_is_conflict(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox = ReplyOutbox(
        event_id=None,
        message_id="om_sent",
        reply_type="text",
        sequence=0,
        transport="feishu",
        payload_json={"text": "already delivered"},
        status=ReplyStatus.SENT.value,
        attempt_count=1,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    async with postgres_session_factory() as session:
        session.add(outbox)
        await session.commit()

    with pytest.raises(DeadLetterConflictError):
        await DeadLetterOpsService(postgres_session_factory).replay(
            "outbox", str(outbox.id), operator="operator", reason="should be rejected"
        )


async def test_replay_rejects_delivered_outbox(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A dead row that already delivered (remote_message_id set) must never be
    replayed: replay would duplicate the sent reply and leave a row the worker
    claim filter never picks up. Only resolve is allowed.
    """
    outbox = ReplyOutbox(
        event_id=None,
        message_id="om_delivered",
        reply_type="text",
        sequence=0,
        transport="feishu",
        payload_json={"text": "delivered once"},
        status=ReplyStatus.DEAD.value,
        attempt_count=1,
        remote_message_id="om_remote_123",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    async with postgres_session_factory() as session:
        session.add(outbox)
        await session.commit()
    outbox_id = str(outbox.id)

    service = DeadLetterOpsService(postgres_session_factory)
    with pytest.raises(DeadLetterConflictError):
        await service.replay(
            "outbox", outbox_id, operator="operator", reason="must be rejected"
        )

    # resolve remains available and audit-only
    result = await service.resolve(
        "outbox", outbox_id, operator="operator", reason="delivered, nothing to replay"
    )
    assert result.outcome == "resolved"
    async with postgres_session_factory() as session:
        row = await session.get(ReplyOutbox, outbox.id)
        audits = (
            await session.scalars(
                select(DeadLetterAction).where(DeadLetterAction.target_id == outbox_id)
            )
        ).all()
    assert row is not None
    assert row.status == ReplyStatus.DEAD.value
    assert len(audits) == 1
    assert audits[0].action == "resolve"


async def test_migration_roundtrip_dead_letter_actions(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade 0027 -> 0028 adds the audit table; downgrade drops it cleanly.

    Runs against a scratch database so the shared test DB is untouched.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_deadletter_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    base_dsn = url.render_as_string(hide_password=False)

    def _run_migrations(target: str) -> None:
        command.upgrade(Config(str(_ALEMBIC_INI)), target)

    def _run_downgrade(target: str) -> None:
        command.downgrade(Config(str(_ALEMBIC_INI)), target)

    async def _has_table(dsn: str) -> bool:
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as connection:
                row = await connection.execute(
                    text("SELECT to_regclass('public.dead_letter_actions')")
                )
                return row.scalar() is not None
        finally:
            await engine.dispose()

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        # CREATE / DROP DATABASE cannot run inside a transaction block.
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(_run_migrations, "20260814_0027")
        assert await _has_table(scratch_dsn) is False

        await asyncio.to_thread(_run_migrations, "head")
        assert await _has_table(scratch_dsn) is True

        await asyncio.to_thread(_run_downgrade, "20260814_0027")
        assert await _has_table(scratch_dsn) is False
    finally:
        await scratch_engine.dispose()
        async with maint_engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
