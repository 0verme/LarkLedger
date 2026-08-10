"""P29 recurring-rule Feishu command parser tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from lark_ledger.models import Direction, RecurringFrequency
from lark_ledger.recurring_commands import (
    RecurringCommandAction,
    try_parse_recurring_command,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_create_monthly() -> None:
    command = try_parse_recurring_command("每月8号房租3500", now=NOW)
    assert command is not None and not isinstance(command, str)
    assert command.action is RecurringCommandAction.CREATE
    assert command.frequency is RecurringFrequency.MONTHLY
    assert command.amount == Decimal("3500")
    assert command.category == "房租"
    assert command.description == "房租"
    assert command.transaction_type is Direction.EXPENSE
    # Today is the 9th, so the next occurrence is next month's 8th.
    assert str(command.next_occurrence) == "2026-09-08"


def test_create_monthly_with_spaces_and_currency() -> None:
    command = try_parse_recurring_command("每月 12 日ChatGPT订阅20 USD", now=NOW)
    assert command is not None and not isinstance(command, str)
    assert command.amount == Decimal("20")
    assert command.currency == "USD"
    assert command.frequency is RecurringFrequency.MONTHLY
    # Today is the 9th, the 12th is still this month.
    assert str(command.next_occurrence) == "2026-08-12"


def test_create_monthly_income_by_keyword() -> None:
    command = try_parse_recurring_command("每月1号工资到账10000", now=NOW)
    assert command is not None and not isinstance(command, str)
    assert command.transaction_type is Direction.INCOME


def test_create_yearly() -> None:
    command = try_parse_recurring_command("每年6月15日保险2000", now=NOW)
    assert command is not None and not isinstance(command, str)
    assert command.frequency is RecurringFrequency.YEARLY
    assert command.amount == Decimal("2000")
    assert str(command.next_occurrence) == "2027-06-15"


def test_create_weekly() -> None:
    command = try_parse_recurring_command("每周健身房100", now=NOW)
    assert command is not None and not isinstance(command, str)
    assert command.frequency is RecurringFrequency.WEEKLY
    assert command.amount == Decimal("100")
    assert str(command.next_occurrence) == "2026-08-16"


def test_create_invalid_amount_returns_guidance() -> None:
    result = try_parse_recurring_command("每月8号房租0", now=NOW)
    assert isinstance(result, str)
    assert "金额无效" in result


def test_list_commands() -> None:
    for text in ("我的周期账单", "周期账单", "查看周期账单", "查询周期账单", "循环账单"):
        command = try_parse_recurring_command(text, now=NOW)
        assert command is not None and not isinstance(command, str)
        assert command.action is RecurringCommandAction.LIST


def test_pause_resume_skip_by_name() -> None:
    pause = try_parse_recurring_command("暂停房租", now=NOW)
    assert pause is not None and not isinstance(pause, str)
    assert pause.action is RecurringCommandAction.PAUSE
    assert pause.name == "房租"

    resume = try_parse_recurring_command("恢复房租", now=NOW)
    assert resume is not None and not isinstance(resume, str)
    assert resume.action is RecurringCommandAction.RESUME
    assert resume.name == "房租"

    skip = try_parse_recurring_command("跳过房租", now=NOW)
    assert skip is not None and not isinstance(skip, str)
    assert skip.action is RecurringCommandAction.SKIP
    assert skip.name == "房租"


def test_skip_current_period_has_no_name() -> None:
    skip = try_parse_recurring_command("跳过本期", now=NOW)
    assert skip is not None and not isinstance(skip, str)
    assert skip.action is RecurringCommandAction.SKIP
    assert skip.name is None


def test_non_recurring_text_falls_through() -> None:
    for text in ("午饭32", "最近10笔", "把 #A83F2 改成35元", "查看 #A83F2"):
        assert try_parse_recurring_command(text, now=NOW) is None


def test_entry_restore_is_not_hijacked() -> None:
    # "恢复 #A83F2" must fall through to the entry-command parser.
    assert try_parse_recurring_command("恢复 #A83F2", now=NOW) is None


def test_near_miss_returns_guidance() -> None:
    result = try_parse_recurring_command("每月8号房租", now=NOW)  # missing amount
    assert isinstance(result, str)
    assert "周期账单" in result

    result = try_parse_recurring_command("我要暂停", now=NOW)  # no rule name
    assert isinstance(result, str)
    assert "暂停" in result
