"""Stable, JSON-serializable Feishu event payloads for claim-time persistence.

Payloads intentionally exclude HTTP headers, signatures, tokens, and SDK objects.
Media binaries are never stored; only resource identifiers inside message content
are retained so a future worker may re-download via the Feishu API.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

PAYLOAD_VERSION: Final[int] = 1

ALLOWED_TRANSPORTS: Final[frozenset[str]] = frozenset({"webhook", "websocket"})


class EventProcessStatus(StrEnum):
    """Minimal status set for v0.2.0 claim-first sync processing."""

    RECEIVED = "received"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LEGACY_SUCCEEDED = "legacy_succeeded"


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
    """Extract the MessageProcessor event dict from a validated payload."""
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise EventPayloadError("payload.event must be an object")
    return normalize_business_event(event)


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
