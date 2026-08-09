from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from lark_ledger.schemas import Action, ParsedCommand

# Account names are matched without digits so the amount can be glued to the
# target name ("支付宝1000元") and still split cleanly; \s* between the target
# name and the amount accepts both "支付宝 100元" and "支付宝100元".
_ARROW_TRANSFER = re.compile(
    r"^\s*(?P<from>[^→>-]{1,64}?)\s*(?:→|->|转到|转入)\s*"
    r"(?P<to>[^\d]{1,64}?)\s*(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:元|块)?\s*$"
)
_FROM_TRANSFER = re.compile(
    r"^\s*(?:从)\s*(?P<from>.{1,64}?)\s*(?:转到|转入|转给)\s*"
    r"(?P<to>[^\d]{1,64}?)\s*(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:元|块)?\s*$"
)


def try_parse_transfer_command(text: str, *, now: datetime) -> ParsedCommand | str | None:
    """Parse common explicit transfer syntax without allowing IDs in chat input."""
    match = _FROM_TRANSFER.fullmatch(text) or _ARROW_TRANSFER.fullmatch(text)
    if match is None:
        return None
    source = match.group("from").strip()
    target = match.group("to").strip()
    if not source or not target:
        return "请使用“招商银行 → 支付宝 1000”并写明两个账户名称和金额。"
    try:
        amount = Decimal(match.group("amount"))
    except InvalidOperation:
        return "转账金额无效，请重新输入。"
    return ParsedCommand(
        action=Action.TRANSFER,
        amount=amount,
        occurred_at=now,
        from_account_hint=source,
        to_account_hint=target,
    )
