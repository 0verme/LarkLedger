"""Deterministic parsing for Feishu account / asset queries (P26/P27).

Account names are hints only: the service resolves them against the current
ledger, so a name that is ambiguous, archived, or belongs to another ledger is
rejected instead of guessed.
"""

from __future__ import annotations

import re
from typing import Final

from lark_ledger.schemas import Action, ParsedCommand

_ACCOUNTS_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:查看|看看)?\s*账户(?:列表)?\s*$", re.IGNORECASE
)
_ACCOUNT_BALANCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:查看|看看)?\s*(?P<name>[^0-9]{1,32}?)\s*(?:账户)?余额\s*$", re.IGNORECASE
)
_ASSETS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:总资产|净资产|总负债|负债|资产|资产负债|我的资产负债|现在.*(?:资产|负债))$",
    re.IGNORECASE,
)


def try_parse_account_command(text: str) -> ParsedCommand | None:
    """Parse a deterministic account/asset query; ``None`` falls through to AI."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    if _ACCOUNTS_LIST_RE.fullmatch(stripped) is not None:
        return ParsedCommand(action=Action.LIST_ACCOUNTS)
    balance_match = _ACCOUNT_BALANCE_RE.fullmatch(stripped)
    if balance_match is not None and not _ASSETS_RE.fullmatch(stripped):
        name = balance_match.group("name").strip()
        if name and not _ASSETS_RE.fullmatch(name):
            return ParsedCommand(action=Action.LIST_ACCOUNTS, account_hint=name)
    if _ASSETS_RE.fullmatch(stripped) is not None:
        return ParsedCommand(action=Action.ASSETS)
    return None
