"""Unified dead-letter query and guarded replay / resolve operations (P44).

Two transport-neutral services over every backlog source:

* ``DeadLetterQueryService`` — one query model for ``processed_events`` /
  ``reply_outbox`` / ``pending_commands`` rows: unified state, bounded error
  classification, replayability assessment, sanitized summaries, resolved
  marker, pagination and detail-with-audit-history.
* ``DeadLetterOpsService`` — the only application-layer entry point for
  dead-letter ``replay`` / ``resolve``. Every action is a row-locked state
  transition (``SELECT ... FOR UPDATE``) plus an append-only
  ``dead_letter_actions`` audit row (operator, reason, before/after status,
  correlated ``request_id``). Replay never executes a remote side effect here:
  it only re-queues (``dead``/``failed`` → ``pending`` for outbox rows, or
  delegates to the existing ``EventReplayService`` for events) and the existing
  workers deliver through the normal lease path.

Neither service touches HTTP, Feishu or the Web layer; the API / admin router
is the only caller. Resolve never deletes source rows and never rewrites their
status — it is a pure audit marker, so the dead-letter lifecycle stays
fully traceable.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger import event_payload
from lark_ledger.dead_letter import (
    DeadLetterDetail,
    DeadLetterReason,
    DeadLetterSource,
    DeadLetterSummary,
    ReplayAssessment,
    assessment_for,
    classify_error_code,
)
from lark_ledger.event_payload import EventProcessStatus
from lark_ledger.models import (
    DeadLetterAction,
    EventReplayAudit,
    PendingCommand,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.outbox import ReplyStatus
from lark_ledger.services.event_replay import EventReplayService

MAX_AUDIT_HISTORY: int = 20


class DeadLetterNotFoundError(ValueError):
    """The requested dead-letter row does not exist (or is already cleaned)."""


class DeadLetterConflictError(ValueError):
    """The requested action is not valid for the row's current state."""


class DeadLetterUnsupportedError(ValueError):
    """The source does not support the requested action."""


#: Unified-state buckets per source status.
_STATE_FOR: dict[str, dict[str, str]] = {
    DeadLetterSource.EVENTS.value: {
        EventProcessStatus.RECEIVED.value: "pending",
        EventProcessStatus.PROCESSING.value: "pending",
        EventProcessStatus.FAILED.value: "retry",
        EventProcessStatus.DEAD.value: "dead",
        EventProcessStatus.SUCCEEDED.value: "terminal",
        EventProcessStatus.LEGACY_SUCCEEDED.value: "terminal",
    },
    DeadLetterSource.OUTBOX.value: {
        ReplyStatus.PENDING.value: "pending",
        ReplyStatus.SENDING.value: "pending",
        ReplyStatus.FAILED.value: "retry",
        ReplyStatus.DEAD.value: "dead",
        ReplyStatus.SENT.value: "terminal",
    },
    DeadLetterSource.PENDING_COMMANDS.value: {
        "pending": "pending",
        "executing": "pending",
        "confirmed": "terminal",
        "executed": "terminal",
        "cancelled": "terminal",
        "expired": "terminal",
        "failed": "terminal",
    },
}

#: Statuses that need operator attention by default (list filter default).
_OPERATOR_STATUSES: frozenset[str] = frozenset({"dead", "failed"})

#: State filter → concrete source statuses.
_STATE_FILTER: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {
            EventProcessStatus.RECEIVED.value,
            EventProcessStatus.PROCESSING.value,
            ReplyStatus.PENDING.value,
            ReplyStatus.SENDING.value,
            "pending",
            "executing",
        }
    ),
    "retry": frozenset(
        {
            EventProcessStatus.FAILED.value,
            ReplyStatus.FAILED.value,
        }
    ),
    "dead": frozenset(
        {
            EventProcessStatus.DEAD.value,
            ReplyStatus.DEAD.value,
        }
    ),
    "resolved": frozenset(),  # resolved is derived from the audit table
    "terminal": frozenset(
        {
            EventProcessStatus.SUCCEEDED.value,
            EventProcessStatus.LEGACY_SUCCEEDED.value,
            ReplyStatus.SENT.value,
            "confirmed",
            "executed",
            "cancelled",
            "expired",
            "failed",
        }
    ),
}


def _mask_identifier(value: str | None) -> str | None:
    """Shorten an external id (message / open id) for safe display."""
    if not value:
        return None
    if len(value) <= 8:
        return value
    return f"{value[:6]}…{value[-4:]}"


#: Credential-bearing key names that must never appear in an API response,
#: even as a key (P44 §7 / §31). Values were already redacted at write time by
#: the worker; this scrubs historical / out-of-band rows too.
_CREDENTIAL_KEY_RE = re.compile(
    r"(?i)\b(authorization|bearer|cookie|password|secret|token|api[_-]?key)\b"
    r"([:=]\s*[^\s,;]+)"
)


def _sanitized_summary(summary: str | None) -> str | None:
    """Re-sanitize a stored error summary before it reaches an API response.

    Values are redacted through the same shape the worker writes; on top of
    that, credential key names are scrubbed entirely so the words themselves
    never leak, even when a historical row stored them verbatim.
    """
    if not summary:
        return None
    text = event_payload.safe_error_summary(
        RuntimeError(summary), max_length=event_payload.MAX_RESULT_SUMMARY_LENGTH
    )
    if not text:
        return None
    return _CREDENTIAL_KEY_RE.sub(r"[redacted]", text)


@dataclass(frozen=True)
class DeadLetterPage:
    items: tuple[DeadLetterSummary, ...]
    page: int
    page_size: int
    total: int
    pages: int


@dataclass(frozen=True)
class DeadLetterActionResult:
    source: str
    target_id: str
    action: str
    outcome: str
    before_status: str | None
    after_status: str | None
    audit_id: str | None
    message: str

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target_id": self.target_id,
            "action": self.action,
            "outcome": self.outcome,
            "before_status": self.before_status,
            "after_status": self.after_status,
            "audit_id": self.audit_id,
            "message": self.message,
        }


class DeadLetterQueryService:
    """Transport-neutral unified dead-letter queries (list / detail)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def list_items(
        self,
        *,
        source: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        retryable: bool | None = None,
        replay_safe: bool | None = None,
        status: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = 25,
        sort: str = "dead_at",
    ) -> DeadLetterPage:
        """Return redacted summaries with optional filters and pagination.

        ``source`` / ``status`` / date-range filter in SQL (riding the existing
        ``(status, ...)`` indexes); derived filters (``reason`` / ``retryable``
        / ``replay_safe``) apply in Python because classification happens after
        loading. Candidates are therefore bounded to operator-relevant rows by
        default (``dead`` / ``failed``); pass an explicit ``status`` or
        ``state`` to widen the scan.
        """
        clean_source = None
        if source:
            clean_source = DeadLetterSource.from_value(source)
        statuses = _resolve_status_filter(state, status)
        async with self._factory() as session:
            if clean_source is None:
                events = await self._list_source(
                    session, DeadLetterSource.EVENTS, statuses, created_from, created_to
                )
                outbox = await self._list_source(
                    session, DeadLetterSource.OUTBOX, statuses, created_from, created_to
                )
                pending = await self._list_source(
                    session, DeadLetterSource.PENDING_COMMANDS, statuses, created_from, created_to
                )
                candidates = [*events, *outbox, *pending]
            else:
                candidates = await self._list_source(
                    session, clean_source, statuses, created_from, created_to
                )

            resolved_keys = await self._resolved_keys(
                session,
                [(item.source, item.id) for item in candidates],
            )
            items = [
                self._with_resolved(item, resolved_keys)
                for item in candidates
                if self._matches_derived(
                    item, reason=reason, retryable=retryable, replay_safe=replay_safe
                )
            ]
            items.sort(key=lambda item: _sort_key(item, sort), reverse=True)
            total = len(items)
            pages = (total + page_size - 1) // page_size if total else 0
            start = (page - 1) * page_size
            return DeadLetterPage(
                items=tuple(items[start : start + page_size]),
                page=page,
                page_size=page_size,
                total=total,
                pages=pages,
            )

    async def detail(self, source: str, target_id: str) -> DeadLetterDetail | None:
        """Return one redacted detail view with audit history (or None)."""
        clean_source = DeadLetterSource.from_value(source)
        clean_id = (target_id or "").strip()
        if not clean_id:
            raise ValueError("target_id is required")
        async with self._factory() as session:
            summary = await self._load_one(session, clean_source, clean_id)
            if summary is None:
                return None
            resolved_keys = await self._resolved_keys(
                session, [(clean_source.value, clean_id)]
            )
            summary = self._with_resolved(summary, resolved_keys)
            audit = await self._audit_history(session, clean_source, clean_id)
            detail_data: dict[str, Any] = {}
            if clean_source is DeadLetterSource.EVENTS:
                event_row = await session.get(ProcessedEvent, clean_id)
                if event_row is not None:
                    detail_data = {
                        "event_id": event_row.event_id,
                        "message_id": _mask_identifier(event_row.source_message_id),
                        "transport": event_row.transport,
                        "lease_owner": event_row.lease_owner,
                        "lease_expires_at": event_row.lease_expires_at,
                        "next_attempt_at": event_row.next_attempt_at,
                        "updated_at": event_row.updated_at,
                    }
            elif clean_source is DeadLetterSource.OUTBOX:
                try:
                    outbox_id = uuid.UUID(clean_id)
                except ValueError as exc:
                    raise ValueError(f"invalid outbox id: {clean_id}") from exc
                outbox_row = await session.get(ReplyOutbox, outbox_id)
                if outbox_row is not None:
                    detail_data = {
                        "event_id": outbox_row.event_id,
                        "message_id": _mask_identifier(outbox_row.message_id),
                        "reply_type": outbox_row.reply_type,
                        "transport": outbox_row.transport,
                        "lease_owner": outbox_row.lease_owner,
                        "lease_expires_at": outbox_row.lease_expires_at,
                        "remote_message_id": _mask_identifier(outbox_row.remote_message_id),
                        "next_attempt_at": outbox_row.next_attempt_at,
                        "updated_at": outbox_row.updated_at,
                    }
            elif clean_source is DeadLetterSource.PENDING_COMMANDS:
                try:
                    pending_id = uuid.UUID(clean_id)
                except ValueError as exc:
                    raise ValueError(f"invalid pending id: {clean_id}") from exc
                pending_row = await session.get(PendingCommand, pending_id)
                if pending_row is not None:
                    detail_data = {
                        "event_id": pending_row.source_event_id,
                        "message_id": _mask_identifier(pending_row.source_message_id),
                        "reply_type": None,
                        "transport": pending_row.transport,
                        "updated_at": pending_row.updated_at,
                    }
            return DeadLetterDetail(
                **DeadLetterDetail.from_summary_fields(summary),
                **detail_data,
                audit=tuple(audit),
            )

    async def _list_source(
        self,
        session: AsyncSession,
        source: DeadLetterSource,
        statuses: frozenset[str] | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> list[DeadLetterSummary]:
        rows: list[Any]
        if source is DeadLetterSource.EVENTS:
            stmt: Select[Any] = select(ProcessedEvent)
            if statuses:
                stmt = stmt.where(ProcessedEvent.status.in_(statuses))
            if created_from is not None:
                stmt = stmt.where(ProcessedEvent.processed_at >= created_from)
            if created_to is not None:
                stmt = stmt.where(ProcessedEvent.processed_at <= created_to)
            rows = list((await session.scalars(stmt)).all())
            return [self._from_event(row) for row in rows]
        if source is DeadLetterSource.OUTBOX:
            stmt = select(ReplyOutbox)
            if statuses:
                stmt = stmt.where(ReplyOutbox.status.in_(statuses))
            if created_from is not None:
                stmt = stmt.where(ReplyOutbox.created_at >= created_from)
            if created_to is not None:
                stmt = stmt.where(ReplyOutbox.created_at <= created_to)
            rows = list((await session.scalars(stmt)).all())
            return [self._from_outbox(row) for row in rows]
        stmt = select(PendingCommand)
        if statuses:
            stmt = stmt.where(PendingCommand.status.in_(statuses))
        if created_from is not None:
            stmt = stmt.where(PendingCommand.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(PendingCommand.created_at <= created_to)
        rows = list((await session.scalars(stmt)).all())
        return [self._from_pending(row) for row in rows]

    async def _load_one(
        self,
        session: AsyncSession,
        source: DeadLetterSource,
        target_id: str,
    ) -> DeadLetterSummary | None:
        if source is DeadLetterSource.EVENTS:
            return await self._load_event(session, target_id)
        if source is DeadLetterSource.OUTBOX:
            return await self._load_outbox(session, target_id)
        return await self._load_pending(session, target_id)

    @staticmethod
    async def _load_event(session: AsyncSession, target_id: str) -> DeadLetterSummary | None:
        row = await session.get(ProcessedEvent, target_id)
        return DeadLetterQueryService._from_event(row) if row is not None else None

    @staticmethod
    async def _load_outbox(session: AsyncSession, target_id: str) -> DeadLetterSummary | None:
        try:
            outbox_id = uuid.UUID(target_id)
        except ValueError:
            return None
        row = await session.get(ReplyOutbox, outbox_id)
        return DeadLetterQueryService._from_outbox(row) if row is not None else None

    @staticmethod
    async def _load_pending(session: AsyncSession, target_id: str) -> DeadLetterSummary | None:
        try:
            pending_id = uuid.UUID(target_id)
        except ValueError:
            return None
        row = await session.get(PendingCommand, pending_id)
        return DeadLetterQueryService._from_pending(row) if row is not None else None

    async def _audit_history(
        self, session: AsyncSession, source: DeadLetterSource, target_id: str
    ) -> list[dict[str, Any]]:
        action_rows = (
            await session.scalars(
                select(DeadLetterAction)
                .where(
                    DeadLetterAction.source == source.value,
                    DeadLetterAction.target_id == target_id,
                )
                .order_by(DeadLetterAction.created_at.desc())
                .limit(MAX_AUDIT_HISTORY)
            )
        ).all()
        history = [
            {
                "action": row.action,
                "operator": row.operator,
                "reason": row.reason,
                "before_status": row.before_status,
                "after_status": row.after_status,
                "error_code": row.error_code,
                "request_id": row.request_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in action_rows
        ]
        if source is DeadLetterSource.EVENTS:
            replay_rows = (
                await session.scalars(
                    select(EventReplayAudit)
                    .where(EventReplayAudit.event_id == target_id)
                    .order_by(EventReplayAudit.created_at.desc())
                    .limit(MAX_AUDIT_HISTORY)
                )
            ).all()
            history.extend(
                {
                    "action": row.action,
                    "operator": row.operator,
                    "reason": row.reason,
                    "before_status": row.previous_status,
                    "after_status": row.resulting_status,
                    "error_code": row.error_code,
                    "request_id": None,
                    "created_at": (
                        row.created_at.isoformat() if row.created_at else None
                    ),
                }
                for row in replay_rows
            )
        return history

    @staticmethod
    def _from_event(row: ProcessedEvent) -> DeadLetterSummary:
        reason = classify_error_code(row.last_error_code, row.result_summary)
        assessment = DeadLetterQueryService._assessment(
            DeadLetterSource.EVENTS, row.status, reason, row
        )
        return DeadLetterSummary(
            source=DeadLetterSource.EVENTS.value,
            id=row.event_id,
            status=row.status,
            state=_STATE_FOR[DeadLetterSource.EVENTS.value].get(row.status, "terminal"),
            created_at=row.received_at or row.processed_at,
            dead_at=row.updated_at,
            attempts=row.attempt_count or 0,
            reason_category=reason.value,
            retryable=assessment.retryable,
            replay_safe=assessment.replay_safe,
            requires_manual_review=assessment.requires_manual_review,
            terminal=assessment.terminal,
            payload_summary=f"event/{row.transport or 'unknown'}",
            last_error_summary=_sanitized_summary(row.result_summary),
        )

    @staticmethod
    def _from_outbox(row: ReplyOutbox) -> DeadLetterSummary:
        reason = classify_error_code(row.last_error_code, row.result_summary)
        assessment = DeadLetterQueryService._assessment(
            DeadLetterSource.OUTBOX,
            row.status,
            reason,
            row,
            remote_message_id=row.remote_message_id,
        )
        return DeadLetterSummary(
            source=DeadLetterSource.OUTBOX.value,
            id=str(row.id),
            status=row.status,
            state=_STATE_FOR[DeadLetterSource.OUTBOX.value].get(row.status, "terminal"),
            created_at=row.created_at,
            dead_at=row.updated_at,
            attempts=row.attempt_count or 0,
            reason_category=reason.value,
            retryable=assessment.retryable,
            replay_safe=assessment.replay_safe,
            requires_manual_review=assessment.requires_manual_review,
            terminal=assessment.terminal,
            payload_summary=f"reply/{row.reply_type}",
            last_error_summary=_sanitized_summary(row.result_summary),
        )

    @staticmethod
    def _from_pending(row: PendingCommand) -> DeadLetterSummary:
        reason = (
            DeadLetterReason.EXPIRED
            if row.status == "expired"
            else DeadLetterReason.UNKNOWN
            if row.status == "failed"
            else DeadLetterReason.UNKNOWN
        )
        assessment = DeadLetterQueryService._assessment(
            DeadLetterSource.PENDING_COMMANDS, row.status, reason, row
        )
        return DeadLetterSummary(
            source=DeadLetterSource.PENDING_COMMANDS.value,
            id=str(row.id),
            status=row.status,
            state=_STATE_FOR[DeadLetterSource.PENDING_COMMANDS.value].get(
                row.status, "terminal"
            ),
            created_at=row.created_at,
            dead_at=row.updated_at,
            attempts=0,
            reason_category=reason.value,
            retryable=assessment.retryable,
            replay_safe=assessment.replay_safe,
            requires_manual_review=assessment.requires_manual_review,
            terminal=assessment.terminal,
            payload_summary=f"command/{row.command_type}",
            last_error_summary=None,
        )

    @staticmethod
    def _assessment(
        source: DeadLetterSource,
        status: str,
        reason: DeadLetterReason,
        row: Any,
        *,
        remote_message_id: str | None = None,
    ) -> ReplayAssessment:
        return assessment_for(
            source=source,
            status=status,
            reason=reason,
            attempts=row.attempt_count if hasattr(row, "attempt_count") else 0,
            remote_message_id=remote_message_id,
        )

    @staticmethod
    def _matches_derived(
        item: DeadLetterSummary,
        *,
        reason: str | None,
        retryable: bool | None,
        replay_safe: bool | None,
    ) -> bool:
        if reason is not None and item.reason_category != reason:
            return False
        if retryable is not None and item.retryable != retryable:
            return False
        if replay_safe is not None and item.replay_safe != replay_safe:
            return False
        return True

    @staticmethod
    def _apply_derived(item: DeadLetterSummary) -> DeadLetterSummary:
        return item

    @staticmethod
    async def _resolved_keys(
        session: AsyncSession, pairs: list[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        """Return the (source, target) pairs with a resolve audit marker."""
        if not pairs:
            return set()
        result = await session.execute(
            select(DeadLetterAction.source, DeadLetterAction.target_id)
            .where(DeadLetterAction.action == "resolve")
            .distinct()
        )
        row_set = {(str(source), str(target)) for source, target in result.all()}
        resolved = set()
        for source_value, target in pairs:
            if (source_value, target) in row_set:
                resolved.add((source_value, target))
        return resolved

    @staticmethod
    def _with_resolved(
        item: DeadLetterSummary, resolved_keys: set[tuple[str, str]]
    ) -> DeadLetterSummary:
        if (item.source, item.id) not in resolved_keys:
            return item
        return DeadLetterSummary(
            source=item.source,
            id=item.id,
            status=item.status,
            state=item.state,
            created_at=item.created_at,
            dead_at=item.dead_at,
            attempts=item.attempts,
            reason_category=item.reason_category,
            retryable=item.retryable,
            replay_safe=item.replay_safe,
            requires_manual_review=item.requires_manual_review,
            terminal=item.terminal,
            payload_summary=item.payload_summary,
            last_error_summary=item.last_error_summary,
            resolved=True,
        )


class DeadLetterOpsService:
    """Guarded replay / resolve with append-only audit (P44).

    Replay never performs the remote side effect itself: it only re-queues the
    row so the existing worker picks it up. Resolve is a pure audit marker —
    source rows are never deleted and their status is never rewritten.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def replay(
        self,
        source: str,
        target_id: str,
        *,
        operator: str,
        reason: str,
        request_id: str | None = None,
    ) -> DeadLetterActionResult:
        clean_source = DeadLetterSource.from_value(source)
        clean_target = self._clean_target(target_id)
        operator = (operator or "").strip()
        reason = (reason or "").strip()
        if not operator:
            raise ValueError("operator is required")
        if not reason:
            raise ValueError("reason is required")

        if clean_source is DeadLetterSource.EVENTS:
            return await self._replay_event(
                clean_target, operator=operator, reason=reason, request_id=request_id
            )
        if clean_source is DeadLetterSource.OUTBOX:
            return await self._replay_outbox(
                clean_target, operator=operator, reason=reason, request_id=request_id
            )
        raise DeadLetterUnsupportedError(
            f"source '{clean_source.value}' does not support replay"
        )

    async def _replay_outbox(
        self,
        outbox_id: str,
        *,
        operator: str,
        reason: str,
        request_id: str | None,
    ) -> DeadLetterActionResult:
        now = datetime.now(UTC)
        try:
            parsed_id = uuid.UUID(outbox_id)
        except ValueError as exc:
            raise DeadLetterNotFoundError(f"invalid outbox id: {outbox_id}") from exc
        async with self._factory() as session:
            row = await session.scalar(
                select(ReplyOutbox)
                .where(ReplyOutbox.id == parsed_id)
                .with_for_update()
            )
            if row is None:
                raise DeadLetterNotFoundError(f"outbox row not found: {outbox_id}")
            if row.status not in (ReplyStatus.DEAD.value, ReplyStatus.FAILED.value):
                raise DeadLetterConflictError(
                    f"outbox row {outbox_id} is '{row.status}', not dead/failed"
                )
            before = row.status
            row.status = ReplyStatus.PENDING.value
            row.next_attempt_at = None
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error_code = None
            row.result_summary = None
            row.updated_at = now
            audit = self._new_action(
                source=DeadLetterSource.OUTBOX,
                target_id=outbox_id,
                action="replay",
                operator=operator,
                reason=reason,
                before_status=before,
                after_status=ReplyStatus.PENDING.value,
                request_id=request_id,
            )
            session.add(audit)
            await session.commit()
            return DeadLetterActionResult(
                source=DeadLetterSource.OUTBOX.value,
                target_id=outbox_id,
                action="replay",
                outcome="requeued",
                before_status=before,
                after_status=ReplyStatus.PENDING.value,
                audit_id=str(audit.id),
                message="回复已重新入队，将由回复 Worker 按正常租约路径投递",
            )

    async def _replay_event(
        self,
        event_id: str,
        *,
        operator: str,
        reason: str,
        request_id: str | None,
    ) -> DeadLetterActionResult:
        # The existing EventReplayService already performs the locked preflight
        # + atomic requeue + its own event_replay_audits row (P06e). We wrap it
        # with the unified audit so every source shares one action log.
        result = await EventReplayService(self._factory).replay(
            event_id, operator=operator, reason=reason, execute=True
        )
        if result.outcome != "requeued":
            codes = (
                ", ".join(result.preflight.reason_codes)
                if result.preflight is not None
                else "unknown"
            )
            raise DeadLetterConflictError(
                f"event {event_id} replay rejected by preflight: {codes}"
            )
        async with self._factory() as session:
            audit = self._new_action(
                source=DeadLetterSource.EVENTS,
                target_id=event_id,
                action="replay",
                operator=operator,
                reason=reason,
                before_status=result.preflight.status if result.preflight else None,
                after_status=result.resulting_status,
                request_id=request_id,
            )
            session.add(audit)
            await session.commit()
            return DeadLetterActionResult(
                source=DeadLetterSource.EVENTS.value,
                target_id=event_id,
                action="replay",
                outcome="requeued",
                before_status=result.preflight.status if result.preflight else None,
                after_status=result.resulting_status,
                audit_id=str(audit.id),
                message="事件已通过安全预检并重新入队",
            )

    async def resolve(
        self,
        source: str,
        target_id: str,
        *,
        operator: str,
        reason: str,
        request_id: str | None = None,
    ) -> DeadLetterActionResult:
        """Acknowledge a dead-letter without replaying it (audit-only).

        Idempotent: resolving an already-resolved target returns
        ``already_resolved`` and writes no second audit row.
        """
        clean_source = DeadLetterSource.from_value(source)
        clean_target = self._clean_target(target_id)
        operator = (operator or "").strip()
        reason = (reason or "").strip()
        if not operator:
            raise ValueError("operator is required")
        if not reason:
            raise ValueError("reason is required")

        async with self._factory() as session:
            row = await self._load_for_resolve(session, clean_source, clean_target)
            if row is None:
                raise DeadLetterNotFoundError(
                    f"{clean_source.value} row not found: {clean_target}"
                )
            before = str(getattr(row, "status", "unknown"))
            existing = await session.scalar(
                select(DeadLetterAction)
                .where(
                    DeadLetterAction.source == clean_source.value,
                    DeadLetterAction.target_id == clean_target,
                    DeadLetterAction.action == "resolve",
                )
                .order_by(DeadLetterAction.created_at.desc())
                .limit(1)
            )
            if existing is not None:
                return DeadLetterActionResult(
                    source=clean_source.value,
                    target_id=clean_target,
                    action="resolve",
                    outcome="already_resolved",
                    before_status=before,
                    after_status=before,
                    audit_id=str(existing.id),
                    message="该 dead-letter 已解决，无需重复操作",
                )
            audit = self._new_action(
                source=clean_source,
                target_id=clean_target,
                action="resolve",
                operator=operator,
                reason=reason,
                before_status=before,
                after_status=before,
                request_id=request_id,
            )
            session.add(audit)
            await session.commit()
            return DeadLetterActionResult(
                source=clean_source.value,
                target_id=clean_target,
                action="resolve",
                outcome="resolved",
                before_status=before,
                after_status=before,
                audit_id=str(audit.id),
                message="已记录解决标记；源记录保留，仅用于审计追溯",
            )

    async def _load_for_resolve(
        self,
        session: AsyncSession,
        source: DeadLetterSource,
        target_id: str,
    ) -> Any | None:
        if source is DeadLetterSource.EVENTS:
            return await session.get(ProcessedEvent, target_id)
        if source is DeadLetterSource.OUTBOX:
            try:
                parsed = uuid.UUID(target_id)
            except ValueError:
                return None
            return await session.get(ReplyOutbox, parsed)
        try:
            parsed = uuid.UUID(target_id)
        except ValueError:
            return None
        return await session.get(PendingCommand, parsed)

    @staticmethod
    def _clean_target(target_id: str) -> str:
        clean = (target_id or "").strip()
        if not clean:
            raise ValueError("target_id is required")
        if len(clean) > 128:
            raise ValueError("target_id is too long")
        return clean

    @staticmethod
    def _new_action(
        *,
        source: DeadLetterSource,
        target_id: str,
        action: str,
        operator: str,
        reason: str,
        before_status: str | None,
        after_status: str | None,
        request_id: str | None,
    ) -> DeadLetterAction:
        return DeadLetterAction(
            source=source.value,
            target_id=target_id,
            action=action,
            operator=operator,
            reason=(reason or "")[:512],
            before_status=(before_status or "")[:32] or None,
            after_status=(after_status or "")[:32] or None,
            request_id=(request_id or "")[:64] or None,
        )


def _resolve_status_filter(
    state: str | None, status: str | None
) -> frozenset[str] | None:
    """Map the unified ``state`` filter to concrete source statuses.

    An explicit ``status`` filter wins over ``state``; ``None`` means no status
    predicate (bounded by the query service's default candidate policy).
    """
    if status:
        return frozenset({status})
    if not state:
        return _OPERATOR_STATUSES
    resolved = _STATE_FILTER.get(state)
    if resolved is None:
        raise ValueError(f"unsupported state filter: {state}")
    return resolved


def _sort_key(item: DeadLetterSummary, sort: str) -> Any:
    if sort == "created_at":
        return item.created_at or datetime.min.replace(tzinfo=UTC)
    if sort == "attempts":
        return item.attempts
    return item.dead_at or item.created_at or datetime.min.replace(tzinfo=UTC)
