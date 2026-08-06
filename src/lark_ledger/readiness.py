"""Local readiness checks for the HTTP process and its background tasks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import State

from lark_ledger.config import EventMode, Settings

logger = logging.getLogger(__name__)

Check = dict[str, Any]


def resolve_code_revision(config_path: str | Path = "alembic.ini") -> tuple[str | None, str | None]:
    """Return the repository's single Alembic head and a safe error code.

    The head is resolved through Alembic rather than duplicated in application
    code. A missing configuration or multiple heads makes the application not
    ready; readiness never attempts to repair or migrate the database.
    """
    try:
        script = ScriptDirectory.from_config(Config(str(config_path)))
        heads = script.get_heads()
    except Exception as exc:
        logger.warning(
            "readiness could not resolve Alembic code head error_code=%s",
            type(exc).__name__,
        )
        return None, "code_head_unavailable"
    if len(heads) != 1:
        return None, "multiple_code_heads" if heads else "code_head_missing"
    return heads[0], None


class ReadinessService:
    """Build a stable, redacted readiness snapshot without external probes."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        expected_revision: str | None = None,
        expected_revision_error: str | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        if expected_revision is None and expected_revision_error is None:
            expected_revision, expected_revision_error = resolve_code_revision()
        self._expected_revision = expected_revision
        self._expected_revision_error = expected_revision_error

    async def check(self, state: State) -> dict[str, Any]:
        """Return independent component checks and the aggregate state."""
        database, migration = await self._check_database_and_migration()
        application = self._application_check(state)
        event_worker = self._worker_check(
            state,
            attribute="event_worker",
            enabled=self._settings.worker_enabled,
        )
        reply_worker = self._worker_check(
            state,
            attribute="reply_worker",
            enabled=self._settings.reply_worker_enabled,
        )
        cleanup_worker = self._cleanup_worker_check(state)
        receiver = self._receiver_check(state)
        checks = {
            "application": application,
            "database": database,
            "migration": migration,
            "event_worker": event_worker,
            "reply_worker": reply_worker,
            "cleanup_worker": cleanup_worker,
            "receiver": receiver,
        }
        ready = all(
            check["status"] in {"ok", "disabled", "warning"}
            for check in checks.values()
        )
        return {"status": "ready" if ready else "not_ready", "checks": checks}

    async def _check_database_and_migration(self) -> tuple[Check, Check]:
        database: Check = {"status": "error", "reason": "database_unavailable"}
        migration: Check = {
            "status": "error",
            "current": None,
            "expected": self._expected_revision,
            "reason": "database_unavailable",
        }
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
                database = {"status": "ok"}
                if self._expected_revision_error is not None:
                    migration["reason"] = self._expected_revision_error
                    return database, migration
                try:
                    rows = await session.execute(text("SELECT version_num FROM alembic_version"))
                    current_revisions = list(rows.scalars())
                except Exception as exc:
                    logger.warning(
                        "readiness migration query failed error_code=%s",
                        type(exc).__name__,
                    )
                    migration["reason"] = "migration_revision_unavailable"
                    return database, migration
        except Exception as exc:
            logger.warning(
                "readiness database probe failed error_code=%s",
                type(exc).__name__,
            )
            return database, migration

        if len(current_revisions) != 1:
            migration["reason"] = (
                "database_uninitialized"
                if not current_revisions
                else "multiple_database_revisions"
            )
            return database, migration
        current = str(current_revisions[0])
        migration["current"] = current
        if current != self._expected_revision:
            migration["reason"] = "migration_revision_mismatch"
            return database, migration
        return database, {
            "status": "ok",
            "current": current,
            "expected": self._expected_revision,
        }

    @staticmethod
    def _application_check(state: State) -> Check:
        if bool(getattr(state, "shutting_down", False)):
            return {"status": "error", "reason": "shutting_down"}
        return {"status": "ok"}

    @staticmethod
    def _worker_check(state: State, *, attribute: str, enabled: bool) -> Check:
        if not enabled:
            return {
                "status": "disabled",
                "enabled": False,
                "started": False,
                "running": False,
                "stopping": False,
                "task_done": False,
                "task_exception": False,
            }
        worker = getattr(state, attribute, None)
        if worker is None:
            return {
                "status": "error",
                "enabled": True,
                "started": False,
                "running": False,
                "stopping": False,
                "task_done": False,
                "task_exception": False,
                "reason": "not_started",
            }
        try:
            snapshot = worker.health_snapshot()
        except Exception as exc:
            logger.warning(
                "readiness worker snapshot failed component=%s error_code=%s",
                attribute,
                type(exc).__name__,
            )
            return {"status": "error", "enabled": True, "reason": "snapshot_unavailable"}
        healthy = bool(
            snapshot.get("started")
            and snapshot.get("running")
            and not snapshot.get("stopping")
            and not snapshot.get("task_done")
            and not snapshot.get("task_exception")
        )
        result: Check = {"status": "ok" if healthy else "error", "enabled": True}
        result.update(snapshot)
        if not healthy:
            result["reason"] = "task_unhealthy"
        return result

    def _receiver_check(self, state: State) -> Check:
        if self._settings.event_mode is EventMode.WEBHOOK:
            return {
                "status": "disabled",
                "mode": EventMode.WEBHOOK.value,
                "started": False,
                "running": False,
                "stopping": False,
                "task_done": False,
                "task_exception": False,
            }
        receiver = getattr(state, "long_connection", None)
        if receiver is None:
            return {
                "status": "error",
                "mode": EventMode.WEBSOCKET.value,
                "started": False,
                "running": False,
                "stopping": False,
                "task_done": False,
                "task_exception": False,
                "reason": "not_started",
            }
        try:
            snapshot = receiver.health_snapshot()
        except Exception as exc:
            logger.warning(
                "readiness receiver snapshot failed error_code=%s",
                type(exc).__name__,
            )
            return {
                "status": "error",
                "mode": EventMode.WEBSOCKET.value,
                "reason": "snapshot_unavailable",
            }
        healthy = bool(
            snapshot.get("started")
            and snapshot.get("running")
            and not snapshot.get("stopping")
            and not snapshot.get("task_done")
            and not snapshot.get("task_exception")
        )
        result: Check = {
            "status": "ok" if healthy else "error",
            "mode": EventMode.WEBSOCKET.value,
        }
        result.update(snapshot)
        if not healthy:
            result["reason"] = "receiver_unhealthy"
        return result

    def _cleanup_worker_check(self, state: State) -> Check:
        result = self._worker_check(
            state,
            attribute="cleanup_worker",
            enabled=self._settings.cleanup_enabled,
        )
        if result["status"] == "error":
            result["status"] = "warning"
            result["reason"] = "cleanup_degraded"
        return result


def startup_incomplete_response() -> dict[str, Any]:
    """Return a stable 503 payload when lifespan has not initialized state."""
    return {
        "status": "not_ready",
        "checks": {
            "application": {"status": "error", "reason": "startup_incomplete"},
        },
    }
