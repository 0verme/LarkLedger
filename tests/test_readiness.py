import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lark_ledger.api import router
from lark_ledger.config import Settings
from lark_ledger.readiness import ReadinessService, resolve_code_revision
from lark_ledger.services.reply_worker import ReplyWorker
from lark_ledger.services.worker import EventWorker


class HealthyTask:
    def health_snapshot(self) -> dict[str, bool | str | None]:
        return {
            "started": True,
            "running": True,
            "stopping": False,
            "task_done": False,
            "task_exception": False,
            "last_error_code": None,
        }


class FailedTask:
    def health_snapshot(self) -> dict[str, bool | str | None]:
        return {
            "started": True,
            "running": False,
            "stopping": False,
            "task_done": True,
            "task_exception": True,
            "last_error_code": "RuntimeError",
        }


class HealthyReceiver(HealthyTask):
    pass


async def sqlite_factory(
    revision: str | None = "20260807_0013",
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        if revision is not None:
            await connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": revision},
            )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def build_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected_revision: str = "20260807_0013",
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.readiness = ReadinessService(
        settings,
        session_factory,
        expected_revision=expected_revision,
    )
    if settings.worker_enabled:
        app.state.event_worker = HealthyTask()
    if settings.reply_worker_enabled:
        app.state.reply_worker = HealthyTask()
    if settings.cleanup_enabled:
        app.state.cleanup_worker = HealthyTask()
    if settings.recurring_enabled:
        app.state.recurring_worker = HealthyTask()
    if settings.event_mode.value == "websocket":
        app.state.long_connection = HealthyReceiver()
    return app


async def get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_healthz_stays_live_without_readiness_or_database() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
        database_url="postgresql+asyncpg://secret.invalid/unavailable",
    )

    response = await get(app, "/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "event_mode": "webhook",
        "long_connection": "disabled",
    }


async def test_readyz_returns_503_before_lifespan_initializes() -> None:
    app = FastAPI()
    app.include_router(router)

    response = await get(app, "/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["application"]["reason"] == "startup_incomplete"


async def test_readyz_returns_200_with_independent_component_checks() -> None:
    engine, factory = await sqlite_factory()
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=True,
        reply_worker_enabled=True,
    )
    response = await get(build_app(settings, factory), "/readyz")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"status": "ok"}
    assert body["checks"]["migration"] == {
        "status": "ok",
        "current": "20260807_0013",
        "expected": "20260807_0013",
    }
    assert body["checks"]["event_worker"]["running"] is True
    assert body["checks"]["reply_worker"]["running"] is True
    assert body["checks"]["receiver"]["status"] == "disabled"
    await engine.dispose()


async def test_readyz_returns_503_for_database_failure_without_leaking_details() -> None:
    class BrokenContext:
        async def __aenter__(self) -> AsyncSession:
            raise RuntimeError("postgresql://operator:secret@private-db.example/ledger")

        async def __aexit__(self, *args: Any) -> None:
            return None

    class BrokenFactory:
        def __call__(self) -> BrokenContext:
            return BrokenContext()

    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.readiness = ReadinessService(
        settings,
        BrokenFactory(),  # type: ignore[arg-type]
        expected_revision="20260807_0013",
    )

    response = await get(app, "/readyz")
    rendered = response.text

    assert response.status_code == 503
    assert response.json()["checks"]["database"]["reason"] == "database_unavailable"
    assert "secret" not in rendered
    assert "private-db" not in rendered


@pytest.mark.parametrize("revision", [None, "20260806_9999"])
async def test_readyz_returns_503_for_uninitialized_or_mismatched_migration(
    revision: str | None,
) -> None:
    engine, factory = await sqlite_factory(revision)
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
    )

    response = await get(build_app(settings, factory), "/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["database"]["status"] == "ok"
    assert response.json()["checks"]["migration"]["status"] == "error"
    await engine.dispose()


@pytest.mark.parametrize("attribute", ["event_worker", "reply_worker"])
async def test_readyz_detects_worker_task_failure(attribute: str) -> None:
    engine, factory = await sqlite_factory()
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=True,
        reply_worker_enabled=True,
    )
    app = build_app(settings, factory)
    setattr(app.state, attribute, FailedTask())

    response = await get(app, "/readyz")
    check = response.json()["checks"][attribute]

    assert response.status_code == 503
    assert check["task_done"] is True
    assert check["task_exception"] is True
    assert check["last_error_code"] == "RuntimeError"
    await engine.dispose()


async def test_disabled_workers_are_valid_and_websocket_receiver_is_required() -> None:
    engine, factory = await sqlite_factory()
    webhook = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
    )
    webhook_response = await get(build_app(webhook, factory), "/readyz")
    assert webhook_response.status_code == 200
    assert webhook_response.json()["checks"]["event_worker"]["status"] == "disabled"

    websocket = Settings(
        _env_file=None,
        event_mode="websocket",
        lark_app_id="app",
        lark_app_secret="secret",
        worker_enabled=False,
        reply_worker_enabled=False,
    )
    app = build_app(websocket, factory)
    del app.state.long_connection
    websocket_response = await get(app, "/readyz")
    assert websocket_response.status_code == 503
    assert websocket_response.json()["checks"]["receiver"]["reason"] == "not_started"
    await engine.dispose()


async def test_readyz_is_not_ready_during_shutdown() -> None:
    engine, factory = await sqlite_factory()
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
    )
    app = build_app(settings, factory)
    app.state.shutting_down = True

    response = await get(app, "/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["application"]["reason"] == "shutting_down"
    await engine.dispose()


async def test_readyz_only_queries_probe_and_alembic_version() -> None:
    engine, factory = await sqlite_factory()
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
    )
    app = build_app(settings, factory)

    responses = await asyncio.gather(*(get(app, "/readyz") for _ in range(4)))

    assert all(response.status_code == 200 for response in responses)
    assert len(statements) == 8
    assert set(statements) == {"select 1", "select version_num from alembic_version"}
    assert not any("processed_events" in statement for statement in statements)
    assert not any("reply_outbox" in statement for statement in statements)
    await engine.dispose()


async def test_event_and_reply_workers_capture_unexpected_task_exits() -> None:
    class EmptyStore:
        async def claim_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    class Processor:
        async def process(self, event_payload: dict[str, Any]) -> None:
            raise AssertionError("no event should be claimed")

    class Deliverer:
        owner_id = "host:1:secret-nonce"
        max_attempts = 3
        retry_base_seconds = 2.0
        retry_max_seconds = 3600.0

    async def fail_sleep(_delay: float) -> None:
        raise RuntimeError("private failure text")

    event_worker = EventWorker(
        EmptyStore(),  # type: ignore[arg-type]
        Processor(),
        owner_id="host:1:secret-nonce",
        sleeper=fail_sleep,
    )
    reply_worker = ReplyWorker(
        EmptyStore(),  # type: ignore[arg-type]
        Deliverer(),  # type: ignore[arg-type]
        owner_id="host:1:secret-nonce",
        sleeper=fail_sleep,
    )
    event_worker.start()
    reply_worker.start()
    for _ in range(4):
        await asyncio.sleep(0)

    for worker in (event_worker, reply_worker):
        snapshot = worker.health_snapshot()
        assert snapshot["running"] is False
        assert snapshot["task_done"] is True
        assert snapshot["task_exception"] is True
        assert snapshot["last_error_code"] == "RuntimeError"
        assert "private failure text" not in str(snapshot)

    await event_worker.stop()
    await reply_worker.stop()


def test_code_revision_is_resolved_from_alembic_configuration() -> None:
    revision, error = resolve_code_revision()

    assert error is None
    assert revision == "20260813_0026"


async def test_cleanup_worker_failure_is_degraded_but_not_a_readiness_failure() -> None:
    engine, factory = await sqlite_factory("20260807_0013")
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
        cleanup_enabled=True,
    )
    app = build_app(settings, factory)
    app.state.cleanup_worker = FailedTask()

    response = await get(app, "/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    cleanup = response.json()["checks"]["cleanup_worker"]
    assert cleanup["status"] == "warning"
    assert cleanup["reason"] == "cleanup_degraded"
    await engine.dispose()
