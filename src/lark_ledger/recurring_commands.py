"""Deterministic Feishu recurring-rule command parser (P29).

Commands are business semantics, never cron: ``每月 8 号房租 3500`` (monthly),
``每年 6 月 15 日保险 2000`` (yearly), ``每周健身房 100`` (weekly). Query and
lifecycle commands identify a rule by its name: ``我的周期账单``,
``暂停房租``, ``恢复房租``, ``跳过房租``. The parser returns a
``RecurringCommand`` for a match, a user-facing ``str`` for a near-miss (reply
and stop), or ``None`` to fall through to the AI interpreter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from lark_ledger.models import Direction, RecurringFrequency
from lark_ledger.services.recurring import first_occurrence_on_day, first_occurrence_on_month_day

_AMOUNT = r"\d+(?:\.\d{1,2})?"
_CURRENCY = r"[A-Za-z]{3}"

#: CREATE: 每月 8 号房租 3500 / 每月12日ChatGPT订阅20 USD / 每月 1 号工资 10000
_CREATE_MONTHLY = re.compile(
    rf"^\s*每月\s*(?P<day>\d{{1,2}})\s*(?:号|日)\s*"
    rf"(?P<name>.{{1,64}}?)\s*(?P<amount>{_AMOUNT})\s*(?P<currency>{_CURRENCY})?\s*$"
)
#: CREATE: 每年 6 月 15 日保险 2000
_CREATE_YEARLY = re.compile(
    rf"^\s*每年\s*(?P<month>\d{{1,2}})\s*月\s*(?P<day>\d{{1,2}})\s*(?:号|日)\s*"
    rf"(?P<name>.{{1,64}}?)\s*(?P<amount>{_AMOUNT})\s*(?P<currency>{_CURRENCY})?\s*$"
)
#: CREATE: 每周健身房100 / 每周健身房 100
_CREATE_WEEKLY = re.compile(
    rf"^\s*每周\s*(?P<name>.{{1,64}}?)\s*(?P<amount>{_AMOUNT})\s*(?P<currency>{_CURRENCY})?\s*$"
)

#: Exact list commands.
_LIST_COMMANDS = frozenset(
    {
        "周期账单",
        "我的周期账单",
        "查看周期账单",
        "查询周期账单",
        "循环账单",
        "我的循环账单",
        "定期账单",
        "我的定期账单",
    }
)
#: Lifecycle commands act on a rule named in the rest of the text.
_PAUSE = re.compile(r"^\s*(?:暂停周期账单|暂停循环账单|暂停账单|暂停)(?P<name>.{1,64}?)\s*$")
_RESUME = re.compile(r"^\s*(?:恢复周期账单|恢复循环账单|恢复账单|恢复)(?P<name>.{1,64}?)\s*$")
_SKIP = re.compile(
    r"^\s*(?:跳过周期账单|跳过循环账单|跳过账单|跳过)(?P<name>.{1,64}?)\s*$"
)

#: Income hints inside the parsed name (工资 / 收入 / 到账 / 退款 ...).
_INCOME_KEYWORDS = ("工资", "收入", "到账", "退款", "报销", "分红", "补贴", "奖金", "稿费")

#: Near-miss guard: text that clearly means recurring but did not parse.
_RECURRING_INTENT = re.compile(r"(周期账单|循环账单|定期账单|每月|每年|每周)")
_NUMBERED_DAY = re.compile(r"每月\s*\d{1,2}\s*(?:号|日)")

_CREATE_GUIDANCE = (
    "没看懂这条周期账单指令。可以用：\n"
    "• 每月8号房租3500\n"
    "• 每年6月15日保险2000\n"
    "• 每周健身房100\n"
    "• 我的周期账单\n"
    "• 暂停房租 / 恢复房租 / 跳过房租"
)


class RecurringCommandAction(StrEnum):
    CREATE = "create"
    LIST = "list"
    PAUSE = "pause"
    RESUME = "resume"
    SKIP = "skip"


@dataclass(frozen=True)
class RecurringCommand:
    action: RecurringCommandAction
    # CREATE fields
    transaction_type: Direction | None = None
    amount: Decimal | None = None
    currency: str | None = None
    category: str | None = None
    description: str | None = None
    frequency: RecurringFrequency | None = None
    next_occurrence: date | None = None
    # Lifecycle: rule identifier parsed from the text (None = this period).
    name: str | None = None


def _parse_amount(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _guess_direction(name: str) -> Direction:
    if any(keyword in name for keyword in _INCOME_KEYWORDS):
        return Direction.INCOME
    return Direction.EXPENSE


def try_parse_recurring_command(text: str, *, now: datetime) -> RecurringCommand | str | None:
    """Parse a deterministic recurring-rule command.

    Returns ``None`` to fall through when the text is not recurring-shaped, a
    user-facing ``str`` for a near-miss (reply and return), or a
    ``RecurringCommand`` for a match. ``now`` must be a timezone-aware datetime
    in the configured application timezone (its ``date()`` is the business date).
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw in _LIST_COMMANDS:
        return RecurringCommand(action=RecurringCommandAction.LIST)

    if not any(
        keyword in raw
        for keyword in ("周期", "循环", "定期", "每月", "每年", "每周", "暂停", "恢复", "跳过")
    ):
        return None

    # Lifecycle commands first (unambiguous; must never reach AI). A name
    # containing ``#`` is an entry / confirmation reference, never a rule name:
    # fall through so the short-ID / confirmation parsers own it.
    match = _PAUSE.fullmatch(raw)
    if match is not None:
        name = match.group("name").strip()
        if not name:
            return "请说明要暂停哪个周期账单，例如：暂停房租"
        if "#" in name:
            return None
        return RecurringCommand(action=RecurringCommandAction.PAUSE, name=name)
    match = _RESUME.fullmatch(raw)
    if match is not None:
        name = match.group("name").strip()
        if not name:
            return "请说明要恢复哪个周期账单，例如：恢复房租"
        if "#" in name:
            return None
        return RecurringCommand(action=RecurringCommandAction.RESUME, name=name)
    match = _SKIP.fullmatch(raw)
    if match is not None:
        name = match.group("name").strip()
        if "#" in name:
            return None
        if not name or name in {"本期", "这个", "这期"}:
            # "跳过本期" targets the most-due rule (resolved by the handler).
            return RecurringCommand(action=RecurringCommandAction.SKIP, name=None)
        return RecurringCommand(action=RecurringCommandAction.SKIP, name=name)

    # CREATE forms.
    match = _CREATE_YEARLY.fullmatch(raw)
    if match is not None:
        return _build_create(match, RecurringFrequency.YEARLY, now)
    match = _CREATE_MONTHLY.fullmatch(raw)
    if match is not None:
        return _build_create(match, RecurringFrequency.MONTHLY, now)
    match = _CREATE_WEEKLY.fullmatch(raw)
    if match is not None:
        return _build_create(match, RecurringFrequency.WEEKLY, now)

    # Near-miss: clearly recurring but unparseable → guide instead of letting
    # the AI write a normal entry for a recurring intent. A bare lifecycle verb
    # is only "recurring-shaped" when it does not reference an entry (``#``).
    if (
        _RECURRING_INTENT.search(raw)
        or _NUMBERED_DAY.search(raw)
        or (re.search(r"(暂停|恢复|跳过)", raw) and "#" not in raw)
    ):
        return _CREATE_GUIDANCE
    return None


def _build_create(
    match: re.Match[str], frequency: RecurringFrequency, now: datetime
) -> RecurringCommand | str:
    name = match.group("name").strip()
    if not name:
        return _CREATE_GUIDANCE
    amount = _parse_amount(match.group("amount"))
    if amount is None or amount <= 0:
        return "周期账单金额无效，请重新输入。"
    currency = (match.group("currency") or "").strip().upper() or None
    day = int(match.group("day")) if "day" in match.re.groupindex else None
    if frequency is RecurringFrequency.YEARLY:
        month = int(match.group("month"))
        if day is None or not 1 <= month <= 12 or not 1 <= day <= 31:
            return "每年日期无效，请使用：每年6月15日保险2000"
        next_date = first_occurrence_on_month_day(now.date(), month, day)
    elif frequency is RecurringFrequency.MONTHLY:
        if day is None or not 1 <= day <= 31:
            return "每月日期无效，请使用：每月8号房租3500"
        next_date = first_occurrence_on_day(now.date(), day)
    else:
        next_date = now.date() + timedelta(days=7)
    return RecurringCommand(
        action=RecurringCommandAction.CREATE,
        transaction_type=_guess_direction(name),
        amount=amount,
        currency=currency,
        category=name,
        description=name,
        frequency=frequency,
        next_occurrence=next_date,
    )
