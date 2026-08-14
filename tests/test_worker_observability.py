"""P42 — worker heartbeat / staleness / restart observability (unit level).

Proves that background workers expose an in-process loop heartbeat:

* ``last_sweep_at`` advances on every loop sweep (including empty ones);
* ``last_success_at`` records a successful processing outcome;
* ``last_error_at`` records failures without leaking the exception text;
* restart resets the heartbeat so stale timestamps never leak across runs;
* readiness reports a wedged loop (task alive, heartbeat frozen) as
  ``warning``/degraded, never as a 503.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from lark_ledger.api import router
from lark_ledger.config import Settings
from lark_ledger.readiness import ReadinessService
from lark_ledger.services.worker import EventWorker, iso_datetime


class EmptyStore:
    async def claim_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class FailingStore:
    async def claim_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("database connection lost")


class Processor:
    async def process(self, event_payload: dict[str, Any]) -> None:
        raise AssertionError("no event should be claimed")


def _clock(now: list[datetime]):
    def clock() -> datetime:
        return now[0]

    return clock


def _worker(now: list[datetime], store: Any | None = None) -> EventWorker:
    return EventWorker(
        store or EmptyStore(),  # type: ignore[arg-type]
        Processor(),
        owner_id="host:1:nonce",
        clock=_clock(now),
        sleeper=lambda _d: asyncio.sleep(0),
    )


async def test_worker_heartbeat_tracks_success_outcomes() -> None:
    now = [datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)]

    class StoreWithEvent:
        async def claim_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
            return [type("C", (), {"event_id": "e1", "attempt_count": 1})()]

        async def load_payload(self, event_id: str) -> dict[str, Any]:
            return {"event_id": event_id}

        async def complete(self, event_id: str, owner_id: str, now: datetime) -> bool:
            return True

    class RecordingProcessor:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, event_payload: dict[str, Any]) -> None:
            self.calls += 1

    worker = EventWorker(
        StoreWithEvent(),  # type: ignore[arg-type]
        RecordingProcessor(),
        owner_id="host:1:nonce",
        clock=_clock(now),
    )
    await worker.run_once(now=now[0])
    snapshot = worker.health_snapshot()

    assert snapshot["last_success_at"] == now[0].isoformat()
    assert snapshot["processed"] == 1


async def test_worker_loop_records_sweep_and_error_heartbeats() -> None:
    now = [datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)]
    worker = _worker(now, store=FailingStore())
    worker.start()
    try:
        for _ in range(3):
            await asyncio.sleep(0)
        snapshot = worker.health_snapshot()
        assert snapshot["running"] is True
        assert snapshot["sweeps"] >= 1
        assert snapshot["last_sweep_at"] == now[0].isoformat()
        assert snapshot["last_error_at"] == now[0].isoformat()
        # Never the raw exception message (privacy / debuggability contract).
        assert "database connection lost" not in str(snapshot)
    finally:
        await worker.stop()


async def test_worker_restart_resets_heartbeat() -> None:
    now = [datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)]
    worker = _worker(now)
    worker.start()
    await worker.stop()

    now[0] = now[0] + timedelta(hours=2)
    worker.start()
    try:
        fresh = worker.health_snapshot()
        assert fresh["last_sweep_at"] is None
        assert fresh["last_success_at"] is None
        assert fresh["last_error_at"] is None
        assert fresh["sweeps"] == 0
        assert fresh["processed"] == 0
    finally:
        await worker.stop()


def test_iso_datetime_serializes_only_real_timestamps() -> None:
    stamp = datetime(2026, 8, 20, tzinfo=UTC)
    assert iso_datetime(stamp) == stamp.isoformat()
    assert iso_datetime(None) is None


class StaleWorker:
    """A task that is alive (running) but whose loop heartbeat is frozen."""

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "started": True,
            "running": True,
            "stopping": False,
            "task_done": False,
            "task_exception": False,
            "last_error_code": None,
            "last_sweep_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        }


class FreshWorker(StaleWorker):
    def health_snapshot(self) -> dict[str, Any]:
        snapshot = super().health_snapshot()
        snapshot["last_sweep_at"] = datetime.now(UTC).isoformat()
        return snapshot


async def _readiness_app(worker: Any) -> FastAPI:
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(
            _text("CREATE TABLE alembic_version (version_num TEXT)")
        )
        await connection.execute(
            _text("INSERT INTO alembic_version VALUES ('20260814_0028')")
        )

    app = FastAPI()
    app.include_router(router)
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=True,
        reply_worker_enabled=False,
        cleanup_enabled=False,
        recurring_enabled=False,
        readiness_stale_after_seconds=30.0,
    )
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.event_worker = worker
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.readiness = ReadinessService(
        settings,
        app.state.session_factory,
        expected_revision="20260814_0028",
    )
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_readyz_reports_stale_worker_as_degraded_not_503() -> None:
    app = await _readiness_app(StaleWorker())

    response = await _get(app, "/readyz")
    body = response.json()

    # A wedged loop is degraded (warning) but must never flip readiness: a
    # restart loop on a stuck-but-alive process would make recovery worse.
    assert body["status"] == "ready"
    assert body["degraded"] is True
    assert body["checks"]["event_worker"]["status"] == "warning"
    assert body["checks"]["event_worker"]["reason"] == "worker_stale"


async def test_readyz_keeps_fresh_worker_ok() -> None:
    app = await _readiness_app(FreshWorker())

    response = await _get(app, "/readyz")
    body = response.json()

    assert body["checks"]["event_worker"]["status"] == "ok"
    assert body["degraded"] is False


class LowFrequencyWorker(StaleWorker):
    """A healthy low-frequency worker whose sweep period exceeds the generic
    stale window (recurring polls every 300s, cleanup every 3600s)."""

    def health_snapshot(self) -> dict[str, Any]:
        snapshot = super().health_snapshot()
        # Swept two minutes ago: stale for a 1s event worker, perfectly
        # healthy for a 300s recurring or 3600s cleanup worker.
        snapshot["last_sweep_at"] = (
            datetime.now(UTC) - timedelta(minutes=2)
        ).isoformat()
        return snapshot


async def _low_frequency_app(worker: Any, enabled: bool) -> FastAPI:
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(
            _text("CREATE TABLE alembic_version (version_num TEXT)")
        )
        await connection.execute(
            _text("INSERT INTO alembic_version VALUES ('20260814_0028')")
        )

    app = FastAPI()
    app.include_router(router)
    settings = Settings(
        _env_file=None,
        event_mode="webhook",
        worker_enabled=False,
        reply_worker_enabled=False,
        cleanup_enabled=enabled,
        recurring_enabled=enabled,
        readiness_stale_after_seconds=30.0,
    )
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.cleanup_worker = worker
    app.state.recurring_worker = worker
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.readiness = ReadinessService(
        settings,
        app.state.session_factory,
        expected_revision="20260814_0028",
    )
    return app


async def test_readyz_does_not_misreport_low_frequency_workers_as_stale() -> None:
    # Regression guard: recurring (300s) and cleanup (3600s) workers sweep far
    # less often than the generic stale window (default 30s). Readiness must
    # scale the stale window to the worker's own sweep period instead of
    # permanently degrading a healthy low-frequency loop.
    app = await _low_frequency_app(LowFrequencyWorker(), enabled=True)

    response = await _get(app, "/readyz")
    body = response.json()

    assert body["status"] == "ready"
    assert body["degraded"] is False
    for component in ("cleanup_worker", "recurring_worker"):
        assert body["checks"][component]["status"] == "ok", component
        assert body["checks"][component].get("reason") is None, component
