"""Dead-letter domain vocabulary and classification (P44).

Transport-neutral vocabulary shared by every pipeline that produces a backlog:

* ``DeadLetterSource`` — which table a dead-letter row lives in.
* ``DeadLetterState`` — the unified lifecycle every source maps onto.
* ``DeadLetterReason`` — the bounded error classification (never a raw
  exception string).
* ``ReplayAssessment`` — whether a dead-letter may be replayed, and at what
  risk.

The module is deliberately dependency-free beyond the standard library and
``event_payload`` (for secret redaction): no SQLAlchemy, no FastAPI, no Feishu
SDK, so both core and domain layers can import it and the architecture guards
stay trivially satisfied.

Classification rules mirror the existing retry policy (``is_permanent_error`` /
``is_permanent_reply_error``): HTTP 408/429/5xx and network-level failures are
retryable, every other 4xx is a remote rejection, payload / database contract
violations are permanent, and anything unrecognized is conservatively
``requires_manual_review`` — never blindly replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from lark_ledger.event_payload import safe_error_summary


#: Source tables understood by the unified dead-letter query model.
class DeadLetterSource(StrEnum):
    EVENTS = "events"
    OUTBOX = "outbox"
    PENDING_COMMANDS = "pending_commands"

    @classmethod
    def from_value(cls, value: str) -> DeadLetterSource:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unsupported dead-letter source: {value!r}") from exc


class DeadLetterState(StrEnum):
    """Unified lifecycle across sources.

    * ``pending`` — awaiting a worker / user.
    * ``retry`` — failed once, a retry is scheduled.
    * ``dead`` — terminal failure, needs operator attention.
    * ``resolved`` — an operator acknowledged it without replaying.
    * ``terminal`` — completed successfully or intentionally stopped; kept here
      so the unified model can still answer "what happened to this id?".
    """

    PENDING = "pending"
    RETRY = "retry"
    DEAD = "dead"
    RESOLVED = "resolved"
    TERMINAL = "terminal"


class DeadLetterReason(StrEnum):
    """Bounded, stable failure classification (never raw exceptions).

    * ``network`` — connect / resolve / transport failures.
    * ``timeout`` — request exceeded a timeout.
    * ``rate_limited`` — remote throttling (HTTP 429 / quota).
    * ``authentication`` — bad / expired credentials (HTTP 401).
    * ``permission`` — authenticated but not allowed (HTTP 403).
    * ``remote_not_found`` — the remote object no longer exists (HTTP 404).
    * ``remote_rejected`` — remote refused the request (HTTP 400 / 422).
    * ``invalid_payload`` — the stored payload cannot be used as-is.
    * ``serialization`` — payload / envelope contract failure.
    * ``database`` — constraint or storage failure.
    * ``business_conflict`` — the action conflicts with current state.
    * ``expired`` — the item is beyond its validity window.
    * ``unknown`` — not classifiable; manual review required.
    """

    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    REMOTE_NOT_FOUND = "remote_not_found"
    REMOTE_REJECTED = "remote_rejected"
    INVALID_PAYLOAD = "invalid_payload"
    SERIALIZATION = "serialization"
    DATABASE = "database"
    BUSINESS_CONFLICT = "business_conflict"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


#: Reasons that are safe to retry after a transient condition clears.
RETRYABLE_REASONS: frozenset[DeadLetterReason] = frozenset(
    {
        DeadLetterReason.NETWORK,
        DeadLetterReason.TIMEOUT,
        DeadLetterReason.RATE_LIMITED,
    }
)

#: Reasons that will reproduce identically on every retry (never replayable).
TERMINAL_REASONS: frozenset[DeadLetterReason] = frozenset(
    {
        DeadLetterReason.REMOTE_REJECTED,
        DeadLetterReason.REMOTE_NOT_FOUND,
        DeadLetterReason.INVALID_PAYLOAD,
        DeadLetterReason.SERIALIZATION,
        DeadLetterReason.EXPIRED,
    }
)

#: Reasons that need a human to decide (auth / permission / data integrity).
MANUAL_REVIEW_REASONS: frozenset[DeadLetterReason] = frozenset(
    {
        DeadLetterReason.AUTHENTICATION,
        DeadLetterReason.PERMISSION,
        DeadLetterReason.DATABASE,
        DeadLetterReason.BUSINESS_CONFLICT,
        DeadLetterReason.UNKNOWN,
    }
)

#: Exception-class names mapped directly to a reason (fast path).
_ERROR_CODE_MAP: dict[str, DeadLetterReason] = {
    # Feishu / httpx transport families.
    "HTTPStatusError": DeadLetterReason.UNKNOWN,  # refined from the summary below
    "ConnectError": DeadLetterReason.NETWORK,
    "ConnectTimeout": DeadLetterReason.NETWORK,
    "ConnectionError": DeadLetterReason.NETWORK,
    "NetworkError": DeadLetterReason.NETWORK,
    "RemoteProtocolError": DeadLetterReason.NETWORK,
    "ReadTimeout": DeadLetterReason.TIMEOUT,
    "WriteTimeout": DeadLetterReason.TIMEOUT,
    "TimeoutError": DeadLetterReason.TIMEOUT,
    "TimeoutException": DeadLetterReason.TIMEOUT,
    "AsyncReadTimeoutError": DeadLetterReason.TIMEOUT,
    "AsyncConnectTimeoutError": DeadLetterReason.TIMEOUT,
    "AIOTimeoutError": DeadLetterReason.TIMEOUT,
    "OpenAITimeoutError": DeadLetterReason.TIMEOUT,
    "APITimeoutError": DeadLetterReason.TIMEOUT,
    # Payload / contract failures.
    "ReplyPayloadError": DeadLetterReason.INVALID_PAYLOAD,
    "EventPayloadError": DeadLetterReason.INVALID_PAYLOAD,
    "ValueError": DeadLetterReason.SERIALIZATION,
    "TypeError": DeadLetterReason.SERIALIZATION,
    "json.JSONDecodeError": DeadLetterReason.SERIALIZATION,
    "JSONDecodeError": DeadLetterReason.SERIALIZATION,
    # Database / integrity.
    "IntegrityError": DeadLetterReason.DATABASE,
    "DataError": DeadLetterReason.DATABASE,
    "OperationalError": DeadLetterReason.DATABASE,
    "DBAPIError": DeadLetterReason.DATABASE,
    # Business state.
    "LeaseLostError": DeadLetterReason.BUSINESS_CONFLICT,
    "ConflictError": DeadLetterReason.BUSINESS_CONFLICT,
    "EntryConflictError": DeadLetterReason.BUSINESS_CONFLICT,
    "TransferConflictError": DeadLetterReason.BUSINESS_CONFLICT,
    "GoalConflictError": DeadLetterReason.BUSINESS_CONFLICT,
    "LedgerAuthorizationError": DeadLetterReason.PERMISSION,
    "PermissionError": DeadLetterReason.PERMISSION,
    "ForbiddenError": DeadLetterReason.PERMISSION,
    "UnauthorizedError": DeadLetterReason.AUTHENTICATION,
    "AuthenticationError": DeadLetterReason.AUTHENTICATION,
    "NotFoundError": DeadLetterReason.REMOTE_NOT_FOUND,
    "FileNotFoundError": DeadLetterReason.REMOTE_NOT_FOUND,
}

#: HTTP status → reason for ``HTTPStatusError`` summaries.
_HTTP_STATUS_REASON: dict[int, DeadLetterReason] = {
    400: DeadLetterReason.REMOTE_REJECTED,
    401: DeadLetterReason.AUTHENTICATION,
    403: DeadLetterReason.PERMISSION,
    404: DeadLetterReason.REMOTE_NOT_FOUND,
    408: DeadLetterReason.TIMEOUT,
    409: DeadLetterReason.BUSINESS_CONFLICT,
    410: DeadLetterReason.REMOTE_NOT_FOUND,
    422: DeadLetterReason.REMOTE_REJECTED,
    429: DeadLetterReason.RATE_LIMITED,
}


def classify_error_code(error_code: str | None, summary: str | None) -> DeadLetterReason:
    """Map a stored ``last_error_code`` (exception class name) to a bounded reason.

    ``HTTPStatusError`` carries its HTTP status in the summary (e.g.
    "Client error '400 Bad Request' for url ..."), which this function parses
    to refine the category; every other code uses the static map and falls back
    to ``unknown``.
    """
    code = (error_code or "").strip()
    if not code:
        return DeadLetterReason.UNKNOWN
    if code == "HTTPStatusError":
        status_code = _http_status_from_summary(summary)
        if status_code is not None:
            if 500 <= status_code < 600:
                # Server-side transient failures are retryable, not terminal.
                return DeadLetterReason.NETWORK
            return _HTTP_STATUS_REASON.get(status_code, DeadLetterReason.REMOTE_REJECTED)
        return DeadLetterReason.UNKNOWN
    return _ERROR_CODE_MAP.get(code, DeadLetterReason.UNKNOWN)


def _http_status_from_summary(summary: str | None) -> int | None:
    """Extract the HTTP status code from an httpx-style error summary."""
    if not summary:
        return None
    # "Client error '400 Bad Request' for url ..." / "Server error '502 ...'"
    for token in summary.split("'")[1::2]:
        first = token.split()[0] if token.split() else ""
        if first.isdigit():
            try:
                return int(first)
            except ValueError:
                continue
    return None


@dataclass(frozen=True)
class ReplayAssessment:
    """Replayability verdict for one dead-letter, derived from its classification.

    ``retryable`` — the failure class may clear on a later attempt.
    ``replay_safe`` — replaying cannot duplicate a committed side effect.
    ``requires_manual_review`` — a human must decide before any action.
    ``terminal`` — replaying is pointless; resolve is the only sensible action.
    ``side_effect_note`` — human-readable (non-secret) caveat for the UI.
    """

    retryable: bool
    replay_safe: bool
    requires_manual_review: bool
    terminal: bool
    side_effect_note: str = ""


@dataclass(frozen=True)
class DeadLetterSummary:
    """Unified, redacted view of one dead-letter across any source.

    Never carries payload content, financial text, tokens or credentials —
    ``payload_summary`` is a bounded low-cardinality descriptor (reply type /
    command type / message type) and ``last_error_summary`` is the sanitized
    single-line error produced by ``safe_error_summary``.
    """

    source: str
    id: str
    status: str
    state: str
    created_at: datetime | None
    dead_at: datetime | None
    attempts: int
    reason_category: str
    retryable: bool
    replay_safe: bool
    requires_manual_review: bool
    terminal: bool
    payload_summary: str
    last_error_summary: str | None
    resolved: bool = False

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "status": self.status,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "dead_at": self.dead_at.isoformat() if self.dead_at else None,
            "attempts": self.attempts,
            "reason_category": self.reason_category,
            "retryable": self.retryable,
            "replay_safe": self.replay_safe,
            "requires_manual_review": self.requires_manual_review,
            "terminal": self.terminal,
            "payload_summary": self.payload_summary,
            "last_error_summary": self.last_error_summary,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class DeadLetterDetail(DeadLetterSummary):
    """Richer per-item view; still redacted (no payload, no secrets)."""

    event_id: str | None = None
    message_id: str | None = None
    reply_type: str | None = None
    transport: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    remote_message_id: str | None = None
    next_attempt_at: datetime | None = None
    updated_at: datetime | None = None
    audit: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_summary_fields(cls, summary: DeadLetterSummary) -> dict[str, Any]:
        """Raw (unserialized) inherited fields of ``summary`` for subclass build."""
        return {
            name: getattr(summary, name)
            for name in cls.__dataclass_fields__
            if name in summary.__dataclass_fields__
        }

    def to_safe_dict(self) -> dict[str, Any]:
        data = super().to_safe_dict()
        data.update(
            {
                "event_id": self.event_id,
                "message_id": self.message_id,
                "reply_type": self.reply_type,
                "transport": self.transport,
                "lease_owner": self.lease_owner,
                "lease_expires_at": (
                    self.lease_expires_at.isoformat() if self.lease_expires_at else None
                ),
                "remote_message_id": self.remote_message_id,
                "next_attempt_at": (
                    self.next_attempt_at.isoformat() if self.next_attempt_at else None
                ),
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "audit": list(self.audit),
            }
        )
        return data


def assessment_for(
    *,
    source: DeadLetterSource,
    status: str,
    reason: DeadLetterReason,
    attempts: int,
    remote_message_id: str | None = None,
    has_business_result: bool | None = None,
) -> ReplayAssessment:
    """Derive the replayability verdict for a classified dead-letter.

    Rules (deliberately conservative — never guess "safe" without evidence):

    * Only ``dead`` / ``failed`` rows are replayable at all; anything else is
      terminal for this operation.
    * Transient categories (network / timeout / rate-limit) are retryable and
      safe **unless** a remote delivery already succeeded (a non-NULL
      ``remote_message_id``), which means a replay may duplicate a side effect.
    * Permanent categories are terminal (never retry, never replay).
    * Auth / permission / database / conflict / unknown need manual review.
    * ``has_business_result=True`` marks event replays that already committed a
      ledger change (the existing ``EventReplayService`` guards the detail).
    """
    if status not in {"dead", "failed"}:
        return ReplayAssessment(
            retryable=False,
            replay_safe=False,
            requires_manual_review=False,
            terminal=True,
            side_effect_note="current status is not replayable",
        )
    if reason in TERMINAL_REASONS:
        return ReplayAssessment(
            retryable=False,
            replay_safe=False,
            requires_manual_review=False,
            terminal=True,
            side_effect_note=f"failure class '{reason.value}' reproduces on every retry",
        )
    if reason in MANUAL_REVIEW_REASONS:
        return ReplayAssessment(
            retryable=False,
            replay_safe=False,
            requires_manual_review=True,
            terminal=False,
            side_effect_note=f"failure class '{reason.value}' requires operator judgment",
        )
    # Retryable categories.
    if remote_message_id:
        return ReplayAssessment(
            retryable=True,
            replay_safe=False,
            requires_manual_review=True,
            terminal=False,
            side_effect_note=(
                "remote_message_id already recorded: a retry may duplicate the "
                "delivered side effect"
            ),
        )
    if has_business_result:
        return ReplayAssessment(
            retryable=True,
            replay_safe=False,
            requires_manual_review=True,
            terminal=False,
            side_effect_note="business result already committed; replay may duplicate it",
        )
    return ReplayAssessment(
        retryable=True,
        replay_safe=True,
        requires_manual_review=False,
        terminal=False,
        side_effect_note="transient failure; a retry uses the existing idempotency key",
    )


def sanitize_error_summary(exc: BaseException) -> str:
    """Sanitize an exception to the stable, secret-redacted summary shape."""
    return safe_error_summary(exc)
