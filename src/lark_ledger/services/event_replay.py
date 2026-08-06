"""Guarded, auditable manual replay for failed event processing.

This service re-opens an original event for business execution. It is not
result replay: any existing reply_outbox row proves that business already
committed and causes a refusal with a pointer to the result-replay path.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    REPLAY_SAFETY_VERSION,
    EventPayloadError,
    EventProcessStatus,
    parse_stored_payload,
)
from lark_ledger.models import EventReplayAudit, LedgerEntry, ProcessedEvent, ReplyOutbox

logger = logging.getLogger(__name__)

MAX_OPERATOR_LENGTH = 128
MAX_REASON_LENGTH = 512
MAX_EVENT_ID_LENGTH = 128


def default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ReplayPreflight:
    """Payload-free operator view of the evidence used by replay policy."""

    event_found: bool
    eligible: bool
    status: str | None
    payload_present: bool
    payload_supported: bool
    replay_contract_proven: bool
    business_result_committed: bool
    source_message_present: bool
    source_message_consistent: bool
    lease_state: str
    outbox_count: int
    outbox_statuses: tuple[str, ...]
    ledger_entry_count: int
    source_item_count: int
    batch_risk: str
    side_effect_proof: str
    previous_attempt_count: int | None
    manual_replay_count: int | None
    reason_codes: tuple[str, ...]
    recommended_action: str

    def to_safe_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventReplayResult:
    mode: str
    outcome: str
    preflight: ReplayPreflight
    audit_id: uuid.UUID | None = None
    resulting_status: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "outcome": self.outcome,
            "audit_id": str(self.audit_id) if self.audit_id is not None else None,
            "resulting_status": self.resulting_status,
            "preflight": self.preflight.to_safe_dict(),
            "will_change": (
                {
                    "status": EventProcessStatus.RECEIVED.value,
                    "attempt_count": 0,
                    "next_attempt_at": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                    "result_summary": None,
                }
                if self.mode == "dry-run" and self.preflight.eligible
                else None
            ),
        }


class EventReplayService:
    """Preflight and atomically requeue one event for a fresh retry window."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def replay(
        self,
        event_id: str,
        *,
        operator: str,
        reason: str,
        execute: bool = False,
        now: datetime | None = None,
    ) -> EventReplayResult:
        clean_event_id, clean_operator, clean_reason = self._validate_request(
            event_id, operator, reason
        )
        current = now or default_clock()
        if current.tzinfo is None:
            raise ValueError("replay clock must be timezone-aware")
        if not execute:
            preflight = await self.preflight(clean_event_id, now=current)
            return EventReplayResult(
                mode="dry-run",
                outcome="eligible" if preflight.eligible else "rejected",
                preflight=preflight,
            )
        return await self._execute(
            clean_event_id,
            operator=clean_operator,
            reason=clean_reason,
            now=current,
        )

    async def preflight(
        self, event_id: str, *, now: datetime | None = None
    ) -> ReplayPreflight:
        clean_event_id = self._validate_event_id(event_id)
        current = now or default_clock()
        if current.tzinfo is None:
            raise ValueError("replay clock must be timezone-aware")
        async with self._factory() as session:
            row = await session.get(ProcessedEvent, clean_event_id)
            return await self._evaluate(session, row, now=current)

    async def _execute(
        self,
        event_id: str,
        *,
        operator: str,
        reason: str,
        now: datetime,
    ) -> EventReplayResult:
        async with self._factory() as session:
            row = await session.scalar(
                select(ProcessedEvent)
                .where(ProcessedEvent.event_id == event_id)
                .with_for_update()
            )
            preflight = await self._evaluate(session, row, now=now)
            if not preflight.eligible:
                audit = self._new_audit(
                    event_id=event_id,
                    operator=operator,
                    reason=reason,
                    row=row,
                    outcome="rejected",
                    resulting_status=row.status if row is not None else None,
                    error_code=preflight.reason_codes[0] if preflight.reason_codes else None,
                    replayed_at=None,
                )
                session.add(audit)
                await session.commit()
                logger.warning(
                    "manual event replay rejected event_id=%s error_code=%s",
                    event_id,
                    audit.error_code,
                )
                return EventReplayResult(
                    mode="execute",
                    outcome="rejected",
                    preflight=preflight,
                    audit_id=audit.id,
                    resulting_status=row.status if row is not None else None,
                )

            if row is None:  # Kept explicit for type narrowing; eligibility forbids it.
                raise RuntimeError("eligible replay event unexpectedly missing")
            previous_attempt_count = row.attempt_count
            row.status = EventProcessStatus.RECEIVED.value
            row.attempt_count = 0
            row.manual_replay_count = (row.manual_replay_count or 0) + 1
            row.next_attempt_at = None
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error_code = None
            row.result_summary = None
            row.updated_at = now
            audit = self._new_audit(
                event_id=event_id,
                operator=operator,
                reason=reason,
                row=row,
                outcome="requeued",
                resulting_status=EventProcessStatus.RECEIVED.value,
                error_code=None,
                replayed_at=now,
                previous_status=preflight.status,
                previous_attempt_count=previous_attempt_count,
            )
            session.add(audit)
            await session.commit()
            logger.info(
                "manual event replay requeued event_id=%s replay_number=%d",
                event_id,
                row.manual_replay_count,
            )
            return EventReplayResult(
                mode="execute",
                outcome="requeued",
                preflight=preflight,
                audit_id=audit.id,
                resulting_status=EventProcessStatus.RECEIVED.value,
            )

    async def _evaluate(
        self,
        session: AsyncSession,
        row: ProcessedEvent | None,
        *,
        now: datetime,
    ) -> ReplayPreflight:
        if row is None:
            return ReplayPreflight(
                event_found=False,
                eligible=False,
                status=None,
                payload_present=False,
                payload_supported=False,
                replay_contract_proven=False,
                business_result_committed=False,
                source_message_present=False,
                source_message_consistent=False,
                lease_state="none",
                outbox_count=0,
                outbox_statuses=(),
                ledger_entry_count=0,
                source_item_count=0,
                batch_risk="unknown",
                side_effect_proof="unproven",
                previous_attempt_count=None,
                manual_replay_count=None,
                reason_codes=("event_not_found",),
                recommended_action="investigate",
            )

        reasons: list[str] = []
        status = row.status
        if status not in {
            EventProcessStatus.DEAD.value,
            EventProcessStatus.FAILED.value,
            EventProcessStatus.PROCESSING.value,
        }:
            reasons.append("status_not_replayable")

        lease_state = self._lease_state(row, now)
        if lease_state in {"active", "ambiguous"}:
            reasons.append("lease_not_safe")
        if status == EventProcessStatus.PROCESSING.value and lease_state != "expired":
            reasons.append("processing_lease_not_expired")

        payload_present = row.payload_json is not None
        parsed: dict[str, Any] | None = None
        payload_supported = False
        if not payload_present:
            reasons.append("payload_missing")
        elif row.payload_version != PAYLOAD_VERSION:
            reasons.append("payload_version_unsupported")
        else:
            try:
                parsed = parse_stored_payload(row.payload_json)
            except EventPayloadError:
                reasons.append("payload_invalid")
            else:
                payload_supported = True
                if parsed["event_id"] != row.event_id:
                    reasons.append("payload_event_mismatch")

        replay_contract_proven = row.replay_safety_version == REPLAY_SAFETY_VERSION
        if not replay_contract_proven:
            reasons.append("atomicity_unproven")

        business_result_committed = row.business_committed_at is not None
        if business_result_committed:
            reasons.append("business_result_committed")

        source_message_present = bool(row.source_message_id and row.source_message_id.strip())
        source_message_consistent = False
        if not source_message_present:
            reasons.append("source_message_missing")
        elif parsed is not None:
            payload_message_id = str(parsed["event"]["message"].get("message_id") or "")
            source_message_consistent = payload_message_id == row.source_message_id
            if not source_message_consistent:
                reasons.append("source_message_mismatch")

        outbox_statuses = tuple(
            (
                await session.scalars(
                    select(ReplyOutbox.status)
                    .where(ReplyOutbox.event_id == row.event_id)
                    .order_by(ReplyOutbox.status)
                )
            ).all()
        )
        if outbox_statuses:
            reasons.append("outbox_exists_use_result_replay")

        source_items: tuple[int | None, ...] = ()
        if source_message_present:
            source_items = tuple(
                (
                    await session.scalars(
                        select(LedgerEntry.source_item_index).where(
                            LedgerEntry.source_message_id == row.source_message_id
                        )
                    )
                ).all()
            )
        if source_items:
            reasons.append("business_result_exists")

        batch_risk = self._batch_risk(parsed, source_items)
        recommended_action = "execute" if not reasons else "investigate"
        if outbox_statuses:
            recommended_action = "replay_result"
        elif source_items or business_result_committed:
            recommended_action = "investigate_duplicate_business_risk"

        return ReplayPreflight(
            event_found=True,
            eligible=not reasons,
            status=status,
            payload_present=payload_present,
            payload_supported=payload_supported,
            replay_contract_proven=replay_contract_proven,
            business_result_committed=business_result_committed,
            source_message_present=source_message_present,
            source_message_consistent=source_message_consistent,
            lease_state=lease_state,
            outbox_count=len(outbox_statuses),
            outbox_statuses=outbox_statuses,
            ledger_entry_count=len(source_items),
            source_item_count=len(set(source_items)),
            batch_risk=batch_risk,
            side_effect_proof=(
                "transactional_outbox_absence"
                if (
                    replay_contract_proven
                    and not business_result_committed
                    and not outbox_statuses
                )
                else "unproven"
            ),
            previous_attempt_count=row.attempt_count,
            manual_replay_count=row.manual_replay_count,
            reason_codes=tuple(dict.fromkeys(reasons)),
            recommended_action=recommended_action,
        )

    @staticmethod
    def _lease_state(row: ProcessedEvent, now: datetime) -> str:
        expiry = row.lease_expires_at
        owner_present = bool(row.lease_owner and row.lease_owner.strip())
        if expiry is None:
            return "ambiguous" if owner_present else "none"
        if expiry.tzinfo is None:
            # SQLite drops timezone metadata in unit tests; PostgreSQL returns
            # an aware value. Stored event timestamps are defined as UTC.
            expiry = expiry.replace(tzinfo=UTC)
        if expiry > now:
            return "active"
        return "expired"

    @staticmethod
    def _batch_risk(
        parsed: dict[str, Any] | None, source_items: tuple[int | None, ...]
    ) -> str:
        if len(source_items) > 1 or any(index not in {None, 0} for index in source_items):
            return "confirmed_existing_batch_result"
        if parsed is None:
            return "unknown"
        message = parsed["event"]["message"]
        message_type = str(message.get("message_type") or "")
        if message_type in {"image", "post", "audio", "file"}:
            return "possible_batch"
        if message_type != "text":
            return "unknown"
        try:
            content = json.loads(str(message.get("content") or "{}"))
        except (TypeError, json.JSONDecodeError):
            return "unknown"
        text = str(content.get("text") or "") if isinstance(content, dict) else ""
        nonempty_lines = [line for line in text.splitlines() if line.strip()]
        return "possible_batch" if len(nonempty_lines) > 1 else "single_or_unknown"

    @staticmethod
    def _new_audit(
        *,
        event_id: str,
        operator: str,
        reason: str,
        row: ProcessedEvent | None,
        outcome: str,
        resulting_status: str | None,
        error_code: str | None,
        replayed_at: datetime | None,
        previous_status: str | None = None,
        previous_attempt_count: int | None = None,
    ) -> EventReplayAudit:
        return EventReplayAudit(
            event_id=event_id,
            operator=operator,
            reason=reason,
            previous_status=(
                previous_status if previous_status is not None else row.status if row else None
            ),
            previous_attempt_count=(
                previous_attempt_count
                if previous_attempt_count is not None
                else row.attempt_count if row else None
            ),
            replay_number=row.manual_replay_count if row else None,
            action="replay_event",
            outcome=outcome,
            resulting_status=resulting_status,
            error_code=error_code[:64] if error_code is not None else None,
            replayed_at=replayed_at,
        )

    @classmethod
    def _validate_request(
        cls, event_id: str, operator: str, reason: str
    ) -> tuple[str, str, str]:
        clean_event_id = cls._validate_event_id(event_id)
        clean_operator = (operator or "").strip()
        clean_reason = (reason or "").strip()
        if not clean_operator:
            raise ValueError("operator is required")
        if len(clean_operator) > MAX_OPERATOR_LENGTH:
            raise ValueError(f"operator must be at most {MAX_OPERATOR_LENGTH} characters")
        if not clean_reason:
            raise ValueError("reason is required")
        if len(clean_reason) > MAX_REASON_LENGTH:
            raise ValueError(f"reason must be at most {MAX_REASON_LENGTH} characters")
        return clean_event_id, clean_operator, clean_reason

    @staticmethod
    def _validate_event_id(event_id: str) -> str:
        clean_event_id = (event_id or "").strip()
        if not clean_event_id:
            raise ValueError("event_id is required")
        if len(clean_event_id) > MAX_EVENT_ID_LENGTH:
            raise ValueError(f"event_id must be at most {MAX_EVENT_ID_LENGTH} characters")
        return clean_event_id
