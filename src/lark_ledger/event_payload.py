"""Stable, JSON-serializable Feishu event payloads for claim-time persistence.

Payloads intentionally exclude HTTP headers, signatures, tokens, and SDK objects.
Media binaries are never stored; only resource identifiers inside message content
are retained so a future worker may re-download via the Feishu API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

PAYLOAD_VERSION: Final[int] = 1

ALLOWED_TRANSPORTS: Final[frozenset[str]] = frozenset({"webhook", "websocket"})

#: Upper bound for ``processed_events.result_summary`` (safe error summaries).
MAX_RESULT_SUMMARY_LENGTH: Final[int] = 512


class EventProcessStatus(StrEnum):
    """Event status set for reliable delivery (v0.2.1 foundation, P05a).

    The sync path writes ``received -> processing -> succeeded|failed``. The
    background worker (P05b) also writes ``dead`` once retries are exhausted or
    an error is permanent. ``legacy_succeeded`` marks pre-payload historical
    rows that are not replayable.

    Since P06a, ``succeeded`` means the business action completed **and** its
    reply intents were durably written to ``reply_outbox``; Feishu delivery
    outcome lives on the outbox rows, so a failed reply never fails the event.
    """

    RECEIVED = "received"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    LEGACY_SUCCEEDED = "legacy_succeeded"


#: States that will never be picked up for processing again.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        EventProcessStatus.SUCCEEDED.value,
        EventProcessStatus.DEAD.value,
        EventProcessStatus.LEGACY_SUCCEEDED.value,
    }
)

#: States the worker may claim, subject to retry / lease windows
#: (``next_attempt_at`` / ``lease_expires_at`` and an attempt budget). Expired
#: ``processing`` rows are also reclaimable; the worker combines this set with
#: the lease-expiry window when it builds its claim predicate.
WORKER_CLAIMABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        EventProcessStatus.RECEIVED.value,
        EventProcessStatus.FAILED.value,
    }
)


class EventPayloadError(ValueError):
    """Raised when a stored or inbound event payload cannot be used safely."""


def normalize_business_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a transport event to the fields MessageProcessor requires."""
    if not isinstance(event, Mapping):
        raise EventPayloadError("event must be an object")

    message = event.get("message")
    if not isinstance(message, Mapping):
        raise EventPayloadError("event.message must be an object")

    message_id = message.get("message_id")
    if message_id is None or str(message_id).strip() == "":
        raise EventPayloadError("event.message.message_id is required")

    content = message.get("content", "{}")
    if content is None:
        content = "{}"
    if isinstance(content, (dict, list)):
        content_str = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(content, str):
        content_str = content
    else:
        raise EventPayloadError("event.message.content must be a string or JSON object")

    message_out: dict[str, Any] = {
        "message_id": str(message_id),
        "message_type": str(message.get("message_type") or ""),
        "content": content_str,
    }
    chat_id = message.get("chat_id")
    if chat_id is not None and str(chat_id).strip() != "":
        message_out["chat_id"] = str(chat_id)

    sender_out: dict[str, Any] = {}
    sender = event.get("sender")
    if isinstance(sender, Mapping):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, Mapping):
            identity: dict[str, str] = {}
            open_id = sender_id.get("open_id")
            user_id = sender_id.get("user_id")
            if open_id is not None and str(open_id).strip() != "":
                identity["open_id"] = str(open_id)
            if user_id is not None and str(user_id).strip() != "":
                identity["user_id"] = str(user_id)
            if identity:
                sender_out["sender_id"] = identity

    return {"sender": sender_out, "message": message_out}


def build_stored_payload(
    event_id: str,
    event: Mapping[str, Any],
    *,
    transport: str,
    received_at: datetime,
) -> dict[str, Any]:
    """Build a versioned envelope ready for JSON persistence."""
    if not event_id or not str(event_id).strip():
        raise EventPayloadError("event_id is required")
    if transport not in ALLOWED_TRANSPORTS:
        raise EventPayloadError(f"unsupported transport: {transport}")
    if received_at.tzinfo is None:
        raise EventPayloadError("received_at must be timezone-aware")

    return {
        "payload_version": PAYLOAD_VERSION,
        "event_id": str(event_id),
        "transport": transport,
        "received_at": received_at.isoformat(),
        "event": normalize_business_event(event),
    }


def serialize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-compatible dict (no pickle, no non-JSON types)."""
    # Round-trip through json to reject non-serializable values early.
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise EventPayloadError("payload is not JSON-serializable") from exc
    if not isinstance(decoded, dict):
        raise EventPayloadError("payload must serialize to an object")
    return decoded


def parse_stored_payload(raw: Mapping[str, Any] | str | None) -> dict[str, Any]:
    """Parse and validate a payload read from the database."""
    if raw is None:
        raise EventPayloadError("legacy event has no payload and is not replayable")
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EventPayloadError("payload_json is not valid JSON") from exc
    elif isinstance(raw, Mapping):
        loaded = dict(raw)
    else:
        raise EventPayloadError("payload_json must be an object or JSON string")

    if not isinstance(loaded, dict):
        raise EventPayloadError("payload_json must be an object")

    version = loaded.get("payload_version")
    if version != PAYLOAD_VERSION:
        raise EventPayloadError(f"unsupported payload_version: {version!r}")

    event_id = loaded.get("event_id")
    if not event_id or not str(event_id).strip():
        raise EventPayloadError("payload.event_id is required")

    transport = loaded.get("transport")
    if transport not in ALLOWED_TRANSPORTS:
        raise EventPayloadError(f"unsupported transport: {transport!r}")

    received_at = loaded.get("received_at")
    if not isinstance(received_at, str) or not received_at:
        raise EventPayloadError("payload.received_at is required")

    event = loaded.get("event")
    if not isinstance(event, Mapping):
        raise EventPayloadError("payload.event must be an object")

    # Re-normalize to enforce the same field whitelist after storage.
    normalized_event = normalize_business_event(event)
    return {
        "payload_version": PAYLOAD_VERSION,
        "event_id": str(event_id),
        "transport": str(transport),
        "received_at": received_at,
        "event": normalized_event,
    }


def business_event_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the MessageProcessor event dict from a validated payload.

    The returned dict carries the source ``event_id`` (from the envelope) so the
    processor can link reply intents to the event and converge a crashed event
    on retry. ``event_id`` is missing only for events delivered directly to the
    processor in tests or out-of-band callers.
    """
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise EventPayloadError("payload.event must be an object")
    business = normalize_business_event(event)
    event_id = payload.get("event_id")
    if event_id is not None and str(event_id).strip():
        business["event_id"] = str(event_id)
    return business


def is_replayable_payload(raw: Mapping[str, Any] | str | None) -> bool:
    """Return True when a DB row can be deserialized for future workers."""
    if raw is None:
        return False
    try:
        parse_stored_payload(raw)
    except EventPayloadError:
        return False
    return True


def message_id_from_event(event: Mapping[str, Any]) -> str | None:
    message = event.get("message")
    if not isinstance(message, Mapping):
        return None
    message_id = message.get("message_id")
    if message_id is None or str(message_id).strip() == "":
        return None
    return str(message_id)


def user_open_id_from_event(event: Mapping[str, Any]) -> str | None:
    """Best-effort sender identifier for denormalized operator lookups.

    Mirrors ``normalize_business_event``: prefers ``open_id``, falls back to
    ``user_id``, and returns ``None`` when the event carries no sender identity.
    """
    sender = event.get("sender")
    if not isinstance(sender, Mapping):
        return None
    sender_id = sender.get("sender_id")
    if not isinstance(sender_id, Mapping):
        return None
    for key in ("open_id", "user_id"):
        value = sender_id.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


_URL_CREDENTIAL_RE = re.compile(r"(://[^/@:\s]+):[^@\s]+@")
_AUTHORIZATION_RE = re.compile(r"(?i)(authorization[:=]\s*).+?(?=[,;]|$)")
_BEARER_TOKEN_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]+")


def _redact_secrets(text: str) -> str:
    """Redact credentials that must never reach the events table."""
    text = _URL_CREDENTIAL_RE.sub(r"\1:***@", text)
    text = _AUTHORIZATION_RE.sub(r"\1***", text)
    text = _BEARER_TOKEN_RE.sub(r"\1***", text)
    return text


def safe_error_summary(
    exc: BaseException, *, max_length: int = MAX_RESULT_SUMMARY_LENGTH
) -> str:
    """Single-line, secret-redacted, length-capped error summary.

    Suitable for ``processed_events.result_summary``. Never includes a
    traceback: ``str(exc)`` is limited to its first line, credentials are
    redacted, and the result is capped at ``max_length`` characters.
    """
    name = type(exc).__name__
    try:
        message = str(exc).strip()
    except Exception:
        # Never let broken __str__ mask the failure we are trying to record.
        message = ""
    if message:
        message = _redact_secrets(message.splitlines()[0])
        summary = f"{name}: {message}"
    else:
        summary = name
    if len(summary) > max_length:
        summary = summary[: max_length - 1] + "…"
    return summary
