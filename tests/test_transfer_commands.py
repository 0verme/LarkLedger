"""Unit tests for the deterministic Feishu transfer command parser.

The parser accepts both spaced and glued account-name + amount forms so natural
input like "招商银行转到支付宝1000元" is recognized without requiring a space.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.transfer_commands import try_parse_transfer_command

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _transfer(text: str) -> tuple[str, Decimal] | None:
    result = try_parse_transfer_command(text, now=NOW)
    assert isinstance(result, ParsedCommand), f"expected a transfer command, got {result!r}"
    assert result.action is Action.TRANSFER
    assert result.occurred_at == NOW
    return result.from_account_hint or "", result.amount or Decimal("0")


@pytest.mark.parametrize(
    "text, expected",
    [
        # Spaced forms keep working (no regression).
        ("招商银行转到支付宝 100元", ("招商银行", Decimal("100"))),
        ("招商银行 → 支付宝 1000", ("招商银行", Decimal("1000"))),
        ("招商银行->支付宝 100元", ("招商银行", Decimal("100"))),
        ("从招商银行转到支付宝 100元", ("招商银行", Decimal("100"))),
        ("从招商银行转入支付宝 100元", ("招商银行", Decimal("100"))),
        ("从招商银行转给支付宝 100元", ("招商银行", Decimal("100"))),
        (" 招商银行 转到 支付宝 100元 ", ("招商银行", Decimal("100"))),
        ("招商银行转到支付宝 100.50元", ("招商银行", Decimal("100.50"))),
        # Glued forms (amount directly after the account name) are now parsed.
        ("招商银行转到支付宝1000元", ("招商银行", Decimal("1000"))),
        ("招商银行转到支付宝100", ("招商银行", Decimal("100"))),
        ("招商银行→支付宝500元", ("招商银行", Decimal("500"))),
        ("从招商银行转入支付宝200元", ("招商银行", Decimal("200"))),
        ("工资卡转到银行卡300元", ("工资卡", Decimal("300"))),
    ],
)
def test_transfer_parser_accepts_spaced_and_glued_forms(
    text: str, expected: tuple[str, Decimal]
) -> None:
    assert _transfer(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty
        "午饭32元",  # ordinary expense, not a transfer
        "招商银行转到支付宝",  # missing amount
        "招商银行转100元",  # missing target account
        "转到支付宝100元",  # missing source account
        "查看账户",  # account query handled elsewhere
        "招商银行账户余额",  # balance query handled elsewhere
    ],
)
def test_transfer_parser_returns_none_for_non_transfers(text: str) -> None:
    assert try_parse_transfer_command(text, now=NOW) is None
