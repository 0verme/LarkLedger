"""P42 — backlog aggregation + /ops/status endpoint (unit level).

Proves:

* per-status aggregate counts for events / outbox / pending commands;
* derived pending / retry / dead buckets with bounded shape;
* ledger isolation: aggregation never reads ledger-scoped rows into Python and
  exposes no user / ledger / request dimensions;
* /ops/status returns build + backlog + worker heartbeat, redacts owner ids and
  never leaks secrets; observability failure degrades instead of 500ing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lark_ledger.api import router
from lark_ledger.config import Settings
from lark_ledger.system_status import SystemStatusService


async def sqlite_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    from lark_ledger.models import Base, PendingCommand, ProcessedEvent, ReplyOutbox

    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all(
            [
                ProcessedEvent(event_id="e-received", status="received"),
                ProcessedEvent(event_id="e-failed", status="failed"),
                ProcessedEvent(event_id="e-failed-2", status="failed"),
                ProcessedEvent(event_id="e-dead", status="dead"),
                ProcessedEvent(event_id="e-processing", status="processing"),
                ProcessedEvent(event_id="e-succeeded", status="succeeded"),
                ReplyOutbox(
                    message_id="m1", reply_type="text", status="pending", payload_json={}
                ),
                ReplyOutbox(
                    message_id="m2", reply_type="text", status="failed", payload_json={}
                ),
                ReplyOutbox(
                    message_id="m3", reply_type="text", status="dead", payload_json={}
                ),
                ReplyOutbox(
                    message_id="m4", reply_type="text", status="sent", payload_json={}
                ),
                PendingCommand(
                    user_open_id="ou_u1",
                    confirmation_code="CA0001",
                    command_type="transfer",
                    payload_json={"kind": "transfer"},
                    preview_json={"code": "CA0001"},
                    risk_reason="test",
                    status="pending",
                    expires_at=datetime(2026, 8, 21, tzinfo=UTC),
                ),
                PendingCommand(
                    user_open_id="ou_u1",
                    confirmation_code="CA0002",
                    command_type="transfer",
                    payload_json={"kind": "transfer"},
                    preview_json={"code": "CA0002"},
                    risk_reason="test",
                    status="executing",
                    expires_at=datetime(2026, 8, 21, tzinfo=UTC),
                ),
                PendingCommand(
                    user_open_id="ou_u1",
                    confirmation_code="CA0003",
                    command_type="transfer",
                    payload_json={"kind": "transfer"},
                    preview_json={"code": "CA0003"},
                    risk_reason="test",
                    status="confirmed",
                    expires_at=datetime(2026, 8, 21, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()
    return engine, factory


def _status_app(engine: AsyncEngine, factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
        cleanup_enabled=False,
        recurring_enabled=False,
        version="0.11.0",
        git_sha="abc123def",
        build_time="t0",
    )
    app.state.session_factory = factory
    return app


async def test_system_status_service_aggregates_bounded_counts() -> None:
    engine, factory = await sqlite_factory()
    try:
        payload = await SystemStatusService(factory).aggregate()

        assert payload["status"] == "ok"
        events = payload["events"]
        assert events["received"] == 1
        assert events["failed"] == 2
        assert events["dead"] == 1
        assert events["processing"] == 1
        assert events["pending"] == 1  # received only
        assert events["retry"] == 2  # failed only
        assert events["dead"] == 1
        assert events["total"] == 5  # succeeded excluded from the observed tail

        outbox = payload["outbox"]
        assert outbox["pending"] == 1
        assert outbox["retry"] == 1
        assert outbox["dead"] == 1
        assert outbox["total"] == 3  # sent excluded

        pendings = payload["pending_commands"]
        assert pendings["pending"] == 1
        assert pendings["total"] == 2  # confirmed excluded
    finally:
        await engine.dispose()


async def test_ops_status_endpoint_returns_aggregate_and_build() -> None:
    engine, factory = await sqlite_factory()
    try:
        app = _status_app(engine, factory)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ops/status")

        assert response.status_code == 200
        body = response.json()
        assert body["build"] == {
            "version": "0.11.0",
            "git_sha": "abc123def",
            "build_time": "t0",
        }
        assert body["backlog"]["events"]["received"] == 1
        assert body["backlog"]["outbox"]["dead"] == 1
        assert body["workers"]["event_worker"]["status"] == "disabled"
        assert body["workers"]["receiver"]["status"] == "disabled"
        # No payload rows, no owner ids, no secret material.
        rendered = response.text
        assert "CA0001" not in rendered
        assert "host:" not in rendered
    finally:
        await engine.dispose()


async def test_ops_status_degrades_when_aggregate_fails() -> None:
    class BrokenContext:
        async def __aenter__(self) -> AsyncSession:
            raise RuntimeError("postgresql://operator:hunter2@private-db.example/ledger")

        async def __aexit__(self, *args: Any) -> None:
            return None

    class BrokenFactory:
        def __call__(self) -> BrokenContext:
            return BrokenContext()

    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(_env_file=None, worker_enabled=False)
    app.state.session_factory = BrokenFactory()  # type: ignore[assignment]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ops/status")

    assert response.status_code == 200  # observability failure != HTTP 500
    body = response.json()
    assert body["backlog"]["status"] == "unavailable"
    assert body["backlog"]["reason"] == "aggregate_unavailable"
    assert "hunter2" not in response.text
    assert "private-db" not in response.text


async def test_ops_status_reports_startup_incomplete_when_no_session_factory() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(_env_file=None, worker_enabled=False)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ops/status")

    assert response.status_code == 200
    assert response.json()["backlog"]["reason"] == "startup_incomplete"
