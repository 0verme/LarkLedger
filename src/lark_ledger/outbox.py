"""Reply outbox envelope and status model (P06a, Transactional Outbox).

A ``reply_outbox`` row is the durable, self-contained intent to send one Feishu
reply. It is written inside the same transaction as the ledger change it
confirms, so a later delivery worker never has to re-execute business, re-call
AI, re-query the ledger, or reopen a temporary file.

This module keeps the domain vocabulary in one place:

* ``ReplyType`` — the kind of Feishu message the row will produce.
* ``ReplyStatus`` — the delivery lifecycle. ``sending`` and ``dead`` are
  reserved for the P06b background worker and are not written in P06a.
* envelope builders — versioned JSON payloads that capture every field the
  sender needs, independent of any in-memory object or session.

Payload versions bump the envelope contract only; existing rows stay readable
because the builder keeps ``payload_version`` on every envelope.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

OUTBOX_PAYLOAD_VERSION: Final[int] = 1

#: Feishu message types the outbox can carry.
class ReplyType(StrEnum):
    TEXT = "text"
    FILE = "file"
    CARD = "card"


class ReplyStatus(StrEnum):
    """Outbox delivery lifecycle.

    * ``pending`` — persisted, delivery not yet confirmed (initial state).
    * ``sending`` — P06b reserved; a worker currently holds the lease.
    * ``sent`` — delivered successfully once; never re-sent.
    * ``failed`` — the single compatible send attempt failed; a P06b worker may
      retry it later.
    * ``dead`` — P06b reserved terminal state after retries are exhausted.
    """

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def build_text_payload(text: str) -> dict[str, Any]:
    """Envelope for a plain text reply. The final text is persisted verbatim."""
    return {
        "payload_version": OUTBOX_PAYLOAD_VERSION,
        "reply_type": ReplyType.TEXT.value,
        "text": text,
    }


def build_file_payload(
    *,
    filename: str,
    content_type: str,
    content: bytes,
) -> dict[str, Any]:
    """Envelope for a file message; raw bytes live in ``payload_blob``.

    ``size`` and ``sha256`` are recorded so a later worker can verify the blob
    survived storage unchanged (e.g. after a container restart).
    """
    return {
        "payload_version": OUTBOX_PAYLOAD_VERSION,
        "reply_type": ReplyType.FILE.value,
        "file": {
            "filename": filename,
            "content_type": content_type,
            "size": len(content),
            "sha256": _sha256_hex(content),
        },
    }


def build_card_payload(
    *,
    card: dict[str, Any],
    image_bytes: bytes | None = None,
    image_alt: str | None = None,
) -> dict[str, Any]:
    """Envelope for an interactive card reply.

    ``card`` is the already-built Feishu card (JSON-serializable dict) with a
    text-only body. When ``image_bytes`` is given the PNG lives in
    ``payload_blob`` and the envelope carries its size, checksum, and the alt
    text needed to inject the image at send time (after the image_key upload).
    """
    envelope: dict[str, Any] = {
        "payload_version": OUTBOX_PAYLOAD_VERSION,
        "reply_type": ReplyType.CARD.value,
        "card": card,
    }
    if image_bytes is not None:
        envelope["image"] = {
            "size": len(image_bytes),
            "sha256": _sha256_hex(image_bytes),
            "alt": image_alt or "",
        }
    else:
        envelope["image"] = None
    return envelope
