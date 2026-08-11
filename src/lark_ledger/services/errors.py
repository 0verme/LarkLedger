"""Transport-neutral error classification utilities.

``is_permanent_error`` was historically defined inside the event worker
(``services/worker.py``) but is used by the domain ``pending`` service as
well. It moved here so domain code never depends on a worker / transport
module — enforcing the v0.9.0 dependency direction:

    Adapter → Application → Domain → Core
"""

from __future__ import annotations

import httpx
import sqlalchemy.exc

from lark_ledger.event_payload import EventPayloadError

#: HTTP codes that usually mean "try again later"; other 4xx are permanent.
_TRANSIENT_HTTP_CODES: frozenset[int] = frozenset({408, 429})


def is_permanent_error(exc: BaseException) -> bool:
    """Conservative, explainable error classification.

    Permanent classes are small and explicit; anything not matched here
    defaults to **retryable** so transient network / AI / Feishu / database
    failures are retried rather than dropped:

    * ``EventPayloadError`` — payload cannot be parsed or is not replayable.
    * ``ValueError`` / ``TypeError`` — a business contract or field error that
      the same input would reproduce forever.
    * ``IntegrityError`` — a duplicate / constraint violation will not resolve
      on retry (and is the ledger's own double-entry guard).
    * Non-408/429 4xx HTTP — explicit client / auth errors (e.g. invalid AI key,
      missing permission) are permanent.

    Unknown errors are retried conservatively up to ``event_max_attempts`` and
    then moved to ``dead``, so a misclassified transient failure is bounded
    rather than retried forever.
    """
    if isinstance(exc, EventPayloadError):
        return True
    if isinstance(exc, (ValueError, TypeError)):
        return True
    if isinstance(exc, sqlalchemy.exc.IntegrityError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code not in _TRANSIENT_HTTP_CODES
    return False
