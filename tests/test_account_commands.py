from __future__ import annotations

from lark_ledger.account_commands import try_parse_account_command
from lark_ledger.schemas import Action, ParsedCommand


def test_list_accounts_parses() -> None:
    for text in ("查看账户", "账户列表", "看看账户", "账户"):
        command = try_parse_account_command(text)
        assert isinstance(command, ParsedCommand)
        assert command.action is Action.LIST_ACCOUNTS
        assert command.account_hint is None


def test_single_balance_parses() -> None:
    command = try_parse_account_command("支付宝余额")
    assert isinstance(command, ParsedCommand)
    assert command.action is Action.LIST_ACCOUNTS
    assert command.account_hint == "支付宝"

    command = try_parse_account_command("查看信用卡余额")
    assert isinstance(command, ParsedCommand)
    assert command.account_hint == "信用卡"


def test_assets_parses() -> None:
    for text in ("总资产", "净资产", "总负债", "资产", "我的资产负债"):
        command = try_parse_account_command(text)
        assert isinstance(command, ParsedCommand)
        assert command.action is Action.ASSETS


def test_non_account_text_falls_through() -> None:
    for text in ("午饭32", "最近10笔", "删除 #A83F2", "用支付宝记支出 20"):
        assert try_parse_account_command(text) is None
