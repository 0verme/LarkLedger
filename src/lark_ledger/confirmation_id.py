"""Confirmation IDs for high-risk pending commands (P07).

Storage form is ``C`` + five Crockford Base32 characters (``CA83F2``).
Display/reference form is ``#C-A83F2``. The ``C`` prefix distinguishes
confirmation codes from ledger entry short IDs (``#A83F2``), and the code is
user-unique and never reused.

Confirmation codes are **not** security credentials: like ledger short IDs they
only select the requesting user's own pendings, and every action re-verifies
``user_open_id``. The code is case-insensitive and parsed by regex only — never
guessed by AI.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

from lark_ledger.short_id import CROCKFORD_ALPHABET, SHORT_ID_LENGTH

CONFIRMATION_PREFIX: Final[str] = "C"
CONFIRMATION_CODE_LENGTH: Final[int] = 1 + SHORT_ID_LENGTH

_CONFIRMATION_REF_RE: Final[re.Pattern[str]] = re.compile(
    rf"^#?{CONFIRMATION_PREFIX}-?"
    rf"([{CROCKFORD_ALPHABET}{CROCKFORD_ALPHABET.lower()}]{{{SHORT_ID_LENGTH}}})$",
    re.IGNORECASE,
)
_CONFIRMATION_CODE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{CONFIRMATION_PREFIX}[{CROCKFORD_ALPHABET}]{{{SHORT_ID_LENGTH}}}$"
)


class ConfirmationCodeError(ValueError):
    """Invalid confirmation code format or allocation failure."""


def generate_confirmation_code() -> str:
    """Return a new confirmation code: ``C`` + five Crockford Base32 characters."""
    return CONFIRMATION_PREFIX + "".join(
        secrets.choice(CROCKFORD_ALPHABET) for _ in range(SHORT_ID_LENGTH)
    )


def format_confirmation_ref(code: str) -> str:
    """Format a stored code for user-facing messages (``#C-A83F2``)."""
    normalized = normalize_confirmation_code(code)
    return f"#{normalized[0]}-{normalized[1:]}"


def normalize_confirmation_code(value: str) -> str:
    """Normalize user or storage input to an uppercase confirmation code.

    Accepts ``#C-A83F2``, ``C-A83F2``, ``#CA83F2``, ``CA83F2`` and lowercase
    variants. Rejects ambiguous characters (I/L/O/U) and any code without the
    ``C`` prefix, so a ledger short ID (``#A83F2``) is never read as a
    confirmation code.
    """
    if not isinstance(value, str):
        raise ConfirmationCodeError("confirmation code must be a string")
    raw = value.strip()
    match = _CONFIRMATION_REF_RE.fullmatch(raw)
    if match is None:
        raise ConfirmationCodeError("确认编号格式无效。请使用例如：确认 #C-A83F2")
    code = CONFIRMATION_PREFIX + match.group(1).upper()
    if not _CONFIRMATION_CODE_RE.fullmatch(code):
        raise ConfirmationCodeError("确认编号包含无效字符")
    return code


def is_valid_confirmation_code(value: str) -> bool:
    try:
        normalize_confirmation_code(value)
    except ConfirmationCodeError:
        return False
    return True
