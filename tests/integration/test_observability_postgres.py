"""P42 PostgreSQL integration: observability against a real database.

Covers the production readiness contract with real PostgreSQL:

* readyz 200 when the database is reachable and the schema matches the head;
* readyz 503 when the migration is behind (expected head differs);
* readyz 503 when the database is unreachable, without leaking connection
  details;
* /ops/status aggregate counts from real rows (events / outbox / pendings);
* /version returns the public build identity.

The schema is created by ``alembic upgrade head`` (CI runs it before this
suite); the ``postgres_engine`` fixture truncates all tables between tests.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lark_ledger.config import Settings
from lark_ledger.main import create_app
from lark_ledger.models import ProcessedEvent, ReplyOutbox
from lark_ledger.readiness import ReadinessService

pytestmark = pytest.mark.postgres


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "event_mode": "webhook",
        "worker_enabled": False,
        "reply_worker_enabled": False,
        "cleanup_enabled": False,
        "recurring_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected_revision: str,
    settings: Settings | None = None,
) -> FastAPI:
    active = settings or _settings()
    # create_app wires the real middleware (request correlation headers) and
    # all routers; lifespan is not run by ASGITransport, so state is seeded
    # explicitly below.
    app = create_app(active)
    app.state.settings = active
    app.state.shutting_down = False
    app.state.session_factory = session_factory
    app.state.readiness = ReadinessService(
        active,
        session_factory,
        expected_revision=expected_revision,
    )
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_readyz_200_when_database_and_migration_are_current(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _app(postgres_session_factory, expected_revision="20260814_0027")

    response = await _get(app, "/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"status": "ok"}
    assert body["checks"]["migration"]["status"] == "ok"
    assert body["checks"]["migration"]["current"] == "20260814_0027"
    assert body["checks"]["migration"]["expected"] == "20260814_0027"
    assert body["degraded"] is False


async def test_readyz_503_when_migration_is_behind(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The real database is at head 20260814_0027; a deployment whose code
    # expects a newer head must not serve traffic until upgraded.
    app = _app(postgres_session_factory, expected_revision="20990101_9999")

    response = await _get(app, "/readyz")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["migration"]["status"] == "error"
    assert body["checks"]["migration"]["reason"] == "migration_revision_mismatch"
    assert body["checks"]["migration"]["current"] == "20260814_0027"


async def test_readyz_503_when_database_is_unreachable(
    postgres_url: str,
) -> None:
    # A reachable-looking URL pointing at a closed port on a private host.
    unreachable = postgres_url.replace(
        "5432", "54329", 1
    )  # nothing listens here
    engine = create_async_engine(unreachable, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app = _app(factory, expected_revision="20260814_0027")

        response = await _get(app, "/readyz")
        body = response.json()
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["status"] == "error"
    assert body["checks"]["database"]["reason"] == "database_unavailable"
    # Never leak the connection URL / credentials.
    assert "postgres" not in response.text.lower() or "asyncpg" not in response.text
    assert "password" not in response.text


async def test_ops_status_aggregates_real_rows(
    postgres_engine: AsyncEngine,
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add_all(
            [
                ProcessedEvent(event_id="pg-received", status="received"),
                ProcessedEvent(event_id="pg-failed", status="failed"),
                ProcessedEvent(event_id="pg-dead", status="dead"),
                ProcessedEvent(event_id="pg-done", status="succeeded"),
                ReplyOutbox(
                    message_id="om_pg_1", reply_type="text", status="pending", payload_json={}
                ),
                ReplyOutbox(
                    message_id="om_pg_2", reply_type="text", status="dead", payload_json={}
                ),
            ]
        )
        await session.commit()

    app = _app(
        postgres_session_factory,
        expected_revision="20260814_0027",
        settings=_settings(version="0.11.0", git_sha="pgsha123", build_time="t"),
    )
    response = await _get(app, "/ops/status")
    body = response.json()

    assert response.status_code == 200
    assert body["build"] == {"version": "0.11.0", "git_sha": "pgsha123", "build_time": "t"}
    assert body["backlog"]["events"]["received"] == 1
    assert body["backlog"]["events"]["failed"] == 1
    assert body["backlog"]["events"]["dead"] == 1
    assert body["backlog"]["events"]["pending"] == 1
    assert body["backlog"]["events"]["retry"] == 1
    # succeeded is the terminal tail and intentionally not counted.
    assert body["backlog"]["events"]["total"] == 3
    assert body["backlog"]["outbox"]["pending"] == 1
    assert body["backlog"]["outbox"]["dead"] == 1
    assert body["backlog"]["outbox"]["total"] == 2
    # No payload rows, no owner ids.
    assert "om_pg_1" not in response.text
    assert "host:" not in response.text


async def test_version_endpoint_on_real_app(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _app(
        postgres_session_factory,
        expected_revision="20260814_0027",
        settings=_settings(version="0.11.0", git_sha="abc123"),
    )

    response = await _get(app, "/version")

    assert response.status_code == 200
    assert response.json()["version"] == "0.11.0"
    assert response.json()["git_sha"] == "abc123"
    # Correlation header present on the version endpoint too.
    assert response.headers.get("x-request-id")
