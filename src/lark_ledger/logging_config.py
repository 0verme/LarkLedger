"""Structured logging and request correlation plumbing (P42).

Establishes a stable, correlatable log contract without rewriting the
project's logging calls:

* every record carries ``request_id`` (``-`` when no HTTP request is in
  flight), populated by a ``contextvars``-backed filter so worker and service
  logs produced inside a request automatically correlate;
* a single idempotent ``setup_logging`` installs the console handler / format
  once at application startup (unit tests drive loggers directly and are
  untouched);
* ``redact_sensitive`` is the explicit escape hatch for message material that
  must never reach logs (tokens, cookies, credentials).

Privacy is enforced by construction here and by source-level guards in the
tests: loggers never receive request headers, cookies, Authorization values,
Feishu secrets, or raw user financial content.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Any

#: The request correlation id in scope for the current async task. Set by the
#: HTTP middleware (``lark_ledger.main``) and read by ``RequestIdFilter`` so
#: every log line emitted while handling a request carries the same id.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

#: Format used when setup_logging installs a handler. ``request_id`` is
#: guaranteed present because ``RequestIdFilter`` runs before formatting.
_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"
)

#: Characters accepted in a client-supplied correlation id. Bounded length
#: prevents header abuse from inflating log lines.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: Maximum length of a server-generated request id.
_GENERATED_REQUEST_ID_LENGTH = 16


class RequestIdFilter(logging.Filter):
    """Attach the in-flight ``request_id`` to every log record.

    Only reliable when installed on the logger that actually emits the
    record (or on a handler): Python logging runs logger filters in
    ``Logger.handle``, so a filter on the root logger is bypassed by
    records propagated up from child loggers. ``RequestIdFormatter`` is
    the robust path used by ``setup_logging``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


class RequestIdFormatter(logging.Formatter):
    """Formatter that injects ``request_id`` before rendering.

    Runs for every record the handler receives, regardless of which logger
    emitted it (child loggers propagate straight to the handler's format
    call without passing through the root logger's ``handle``), so the
    ``request_id=`` field in ``_LOG_FORMAT`` is always populated.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = request_id_var.get() or "-"
        return super().format(record)


def normalize_request_id(value: str | None) -> str | None:
    """Return ``value`` when it is a safe correlation id, else ``None``.

    A client may supply a correlation id for traceability, but only well-formed
    ids are accepted: at most 128 chars from ``[A-Za-z0-9._-]``. Anything else
    (oversized, binary, whitespace) is rejected and the caller must generate a
    fresh server id instead.
    """
    if not value:
        return None
    if _REQUEST_ID_PATTERN.fullmatch(value) is None:
        return None
    return value


def generate_request_id() -> str:
    """Return a compact, unguessable server-generated correlation id."""
    import uuid

    return uuid.uuid4().hex[:_GENERATED_REQUEST_ID_LENGTH]


def set_request_id(request_id: str) -> Any:
    """Bind ``request_id`` for the current async context; returns a reset token."""
    return request_id_var.set(request_id)


def reset_request_id(token: Any) -> None:
    request_id_var.reset(token)


def redact_sensitive(text: str) -> str:
    """Best-effort scrub of credential-shaped material from log text.

    Covers the shapes this project can encounter: ``Bearer <token>``,
    ``Authorization: ...``, ``token=...``, ``secret=...``, ``password=...``,
    ``api_key=...`` and long opaque hex/base64 strings. This is a safety net —
    callers should still avoid logging sensitive material in the first place.
    """
    # Specific shapes first: a bearer token inside an Authorization header must
    # be consumed by the bearer rule, then the header itself by the auth rule.
    redacted = re.sub(
        r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]+)", r"\1 [redacted]", text
    )
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*)([^,;]+)", r"\1[redacted]", redacted
    )
    redacted = re.sub(
        r"(?i)\b(token|secret|password|api[_-]?key|credential)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[redacted]",
        redacted,
    )
    redacted = re.sub(r"\b([A-Za-z0-9+/]{32,}={0,2})\b", "[redacted]", redacted)
    return redacted


def setup_logging() -> None:
    """Idempotently install the console handler, level, and request-id format.

    ``RequestIdFormatter`` (not a root-level filter) is used because Python
    only applies logger filters in ``Logger.handle`` — child loggers such as
    ``lark_ledger.services.*`` propagate to the root handler's formatter
    without ever visiting the root logger's filter, which would crash the
    ``%(request_id)s`` format with ``KeyError``.
    """
    root = logging.getLogger()
    has_handler = any(
        isinstance(handler, logging.StreamHandler) for handler in root.handlers
    )
    if not has_handler:
        handler = logging.StreamHandler()
        handler.setFormatter(RequestIdFormatter(_LOG_FORMAT))
        root.addHandler(handler)
    else:
        for existing in root.handlers:
            if isinstance(existing, logging.StreamHandler) and not isinstance(
                existing.formatter, RequestIdFormatter
            ):
                existing.setFormatter(RequestIdFormatter(_LOG_FORMAT))
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    if not any(isinstance(f, RequestIdFilter) for f in root.filters):
        root.addFilter(RequestIdFilter())
