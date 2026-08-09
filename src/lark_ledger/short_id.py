"""Ledger-scoped entry short IDs for chat references.

Storage form is five Crockford Base32 characters without a leading ``#``.
Display/reference form is ``#XXXXX``. Short IDs are not security credentials;
callers must always combine them with a server-authorized ``ledger_id``.
"""

from __future__ import annotations

import re
import secrets
from typing import Final

# Crockford Base32 without I, L, O, U (ambiguous with 1/0).
CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_ID_LENGTH: Final[int] = 5
MAX_SHORT_ID_ALLOCATION_ATTEMPTS: Final[int] = 16

_SHORT_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[{CROCKFORD_ALPHABET}]{{{SHORT_ID_LENGTH}}}$"
)
_REF_RE: Final[re.Pattern[str]] = re.compile(
    rf"^#?([{CROCKFORD_ALPHABET}{CROCKFORD_ALPHABET.lower()}]{{{SHORT_ID_LENGTH}}})$"
)


class ShortIdError(ValueError):
    """Invalid short ID format or allocation failure."""


def generate_short_id() -> str:
    """Return a new random five-character Crockford Base32 short ID."""
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(SHORT_ID_LENGTH))


def format_entry_ref(short_id: str) -> str:
    """Format a stored short ID for user-facing messages (``#XXXXX``)."""
    normalized = normalize_entry_ref(short_id)
    return f"#{normalized}"


def normalize_entry_ref(value: str) -> str:
    """Normalize user or storage input to a five-character uppercase short ID.

    Accepts optional leading ``#`` and lowercase letters. Does not fuzzy-correct
    ambiguous characters (e.g. ``O`` is rejected, not mapped to ``0``).
    """
    if not isinstance(value, str):
        raise ShortIdError("short ID must be a string")
    raw = value.strip()
    match = _REF_RE.fullmatch(raw)
    if match is None:
        raise ShortIdError("short ID must be exactly five Crockford Base32 characters")
    normalized = match.group(1).upper()
    # Reject I,L,O,U even if somehow present after uppercasing non-crockford.
    if not _SHORT_ID_RE.fullmatch(normalized):
        raise ShortIdError("short ID contains invalid characters")
    return normalized


def is_valid_short_id(value: str) -> bool:
    try:
        normalize_entry_ref(value)
    except ShortIdError:
        return False
    return True


_EXTRACT_RE: Final[re.Pattern[str]] = re.compile(
    rf"#?([{CROCKFORD_ALPHABET}{CROCKFORD_ALPHABET.lower()}]{{{SHORT_ID_LENGTH}}})"
)


def extract_entry_refs(text: str) -> list[str]:
    """Return normalized short IDs found in text, in order of appearance (deduped)."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _EXTRACT_RE.finditer(text or ""):
        raw = match.group(0)
        try:
            code = normalize_entry_ref(raw)
        except ShortIdError:
            continue
        if code not in seen:
            seen.add(code)
            found.append(code)
    return found
