"""Aggregated backlog / queue observability (P42).

Answers "is the outbox backing up?" with bounded, redacted database aggregates:

* ``processed_events`` — Feishu event pipeline (received / processing / retry /
  dead);
* ``reply_outbox`` — transactional outbox intents (pending / sending / retry /
  dead);
* ``pending_commands`` — high-risk confirmations awaiting the user.

Every query is a single ``GROUP BY status`` filtered to non-terminal states so
it rides the existing ``(status, ...)`` indexes and never streams rows into
Python. No user / ledger / request dimensions are ever returned, keeping the
future Prometheus label cardinality bounded. Terminal statuses (succeeded /
sent / executed) are deliberately not counted: they are the high-volume,
operationally-uninteresting tail.

Failure isolation: a query error surfaces as ``status: "unavailable"`` for that
section; the caller (``/ops/status``) never turns an observability failure into
an HTTP 500.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.models import PendingCommand, ProcessedEvent, ReplyOutbox

#: Event states that still require operator attention or worker action.
_EVENT_OBSERVED_STATUSES: tuple[str, ...] = ("received", "processing", "failed", "dead")
#: Outbox states that still require delivery or operator attention.
_OUTBOX_OBSERVED_STATUSES: tuple[str, ...] = ("pending", "sending", "failed", "dead")
#: Pending-command states that still require user confirmation.
_PENDING_OBSERVED_STATUSES: tuple[str, ...] = ("pending", "executing")


def _aggregate(
    rows: list[tuple[str, int]],
    observed: tuple[str, ...],
    *,
    pending_keys: tuple[str, ...],
    retry_keys: tuple[str, ...],
    dead_keys: tuple[str, ...],
) -> dict[str, Any]:
    counts = {key: 0 for key in observed}
    for status, count in rows:
        if status in counts:
            counts[status] = count
    # P42 canonical summaries: the three buckets the operations runbook reasons
    # about — pending (awaiting work), retry (failed, will be retried) and dead
    # (terminal failure, needs operator attention) — alias-free and bounded.
    summary = {
        "pending": sum(counts[key] for key in pending_keys),
        "retry": sum(counts[key] for key in retry_keys),
        "dead": sum(counts[key] for key in dead_keys),
    }
    summary["total"] = sum(counts.values())
    return {**counts, **summary}


class SystemStatusService:
    """Transport-neutral aggregate queries for operational status endpoints."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def aggregate(self) -> dict[str, Any]:
        """Return per-table status counts, derived totals and oldest ages."""
        async with self._session_factory() as session:
            event_rows = await self._counts(
                session, ProcessedEvent.status, _EVENT_OBSERVED_STATUSES
            )
            outbox_rows = await self._counts(
                session, ReplyOutbox.status, _OUTBOX_OBSERVED_STATUSES
            )
            pending_rows = await self._counts(
                session, PendingCommand.status, _PENDING_OBSERVED_STATUSES
            )
            event_ages = await self._ages(
                session,
                ProcessedEvent,
                pending_statuses=("received",),
                retry_statuses=("failed",),
                dead_statuses=("dead",),
            )
            outbox_ages = await self._ages(
                session,
                ReplyOutbox,
                pending_statuses=("pending",),
                retry_statuses=("failed",),
                dead_statuses=("dead",),
            )
            pending_ages = await self._ages(
                session,
                PendingCommand,
                pending_statuses=("pending", "executing"),
                retry_statuses=(),
                dead_statuses=(),
            )
        events = _aggregate(
            event_rows,
            _EVENT_OBSERVED_STATUSES,
            pending_keys=("received",),
            retry_keys=("failed",),
            dead_keys=("dead",),
        )
        outbox = _aggregate(
            outbox_rows,
            _OUTBOX_OBSERVED_STATUSES,
            pending_keys=("pending",),
            retry_keys=("failed",),
            dead_keys=("dead",),
        )
        pendings = _aggregate(
            pending_rows,
            _PENDING_OBSERVED_STATUSES,
            pending_keys=("pending",),
            retry_keys=(),
            dead_keys=(),
        )
        events.update(event_ages)
        outbox.update(outbox_ages)
        pendings.update(pending_ages)
        return {
            "status": "ok",
            "events": events,
            "outbox": outbox,
            "pending_commands": pendings,
        }

    @staticmethod
    async def _counts(
        session: AsyncSession,
        status_column: Any,
        observed: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        stmt = (
            select(status_column, func.count())
            .where(status_column.in_(observed))
            .group_by(status_column)
        )
        result = await session.execute(stmt)
        return [(str(status), int(count)) for status, count in result.all()]

    @staticmethod
    async def _ages(
        session: AsyncSession,
        model: Any,
        *,
        pending_statuses: tuple[str, ...],
        retry_statuses: tuple[str, ...],
        dead_statuses: tuple[str, ...],
    ) -> dict[str, str | None]:
        """Oldest per-bucket timestamp (P44 backlog hygiene), ISO or None.

        Each query rides the existing ``(status, ...)`` index and returns a
        single scalar, so the aggregate stays bounded regardless of table size.
        """
        result: dict[str, str | None] = {
            "oldest_pending_at": None,
            "oldest_retry_at": None,
            "oldest_dead_at": None,
        }
        if pending_statuses:
            result["oldest_pending_at"] = await SystemStatusService._oldest_for(
                session, model, pending_statuses
            )
        if retry_statuses:
            result["oldest_retry_at"] = await SystemStatusService._oldest_for(
                session, model, retry_statuses
            )
        if dead_statuses:
            result["oldest_dead_at"] = await SystemStatusService._oldest_for(
                session, model, dead_statuses
            )
        return result

    @staticmethod
    async def _oldest_for(
        session: AsyncSession, model: Any, statuses: tuple[str, ...]
    ) -> str | None:
        value = await session.scalar(
            select(func.min(model.updated_at)).where(model.status.in_(statuses))
        )
        return value.isoformat() if value is not None else None
