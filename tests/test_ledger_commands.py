from __future__ import annotations

from lark_ledger.ledger_commands import (
    LedgerCommand,
    LedgerCommandAction,
    try_parse_ledger_command,
)


def test_create_accepts_synonyms() -> None:
    for text in ("创建账本 旅行", "新建账本 旅行", "创建新账本 旅行"):
        command = try_parse_ledger_command(text)
        assert isinstance(command, LedgerCommand)
        assert command.action is LedgerCommandAction.CREATE
        assert command.name == "旅行"


def test_select_and_default_accept_synonyms() -> None:
    for text in ("切换账本 旅行", "切换到账本 旅行"):
        command = try_parse_ledger_command(text)
        assert isinstance(command, LedgerCommand)
        assert command.action is LedgerCommandAction.SELECT
        assert command.name == "旅行"
    for text in ("设为默认账本 旅行", "设置默认账本 旅行"):
        command = try_parse_ledger_command(text)
        assert isinstance(command, LedgerCommand)
        assert command.action is LedgerCommandAction.SET_DEFAULT
        assert command.name == "旅行"


def test_no_arg_and_rename() -> None:
    for text, expected in (
        ("账本列表", LedgerCommandAction.LIST),
        ("当前账本", LedgerCommandAction.CURRENT),
        ("查看账本", LedgerCommandAction.LIST),
        ("我的账本", LedgerCommandAction.LIST),
    ):
        command = try_parse_ledger_command(text)
        assert isinstance(command, LedgerCommand)
        assert command.action is expected
    command = try_parse_ledger_command("重命名账本 旅行 旅游")
    assert isinstance(command, LedgerCommand)
    assert command.action is LedgerCommandAction.RENAME
    assert command.name == "旅行" and command.new_name == "旅游"


def test_missing_name_returns_usage_hint() -> None:
    for text in ("创建账本", "新建账本", "切换账本", "设为默认账本"):
        result = try_parse_ledger_command(text)
        assert isinstance(result, str)
        assert "用法" in result


def test_unmatched_ledger_intent_returns_ledger_usage_not_none() -> None:
    for text in ("账本 123", "查看账本 旅行", "当前账本是哪个"):
        result = try_parse_ledger_command(text)
        assert isinstance(result, str)
        assert "账本命令用法" in result


def test_non_ledger_text_falls_through() -> None:
    for text in ("午饭32", "最近10笔", "用招商银行付午饭 32", "这个月花了多少"):
        assert try_parse_ledger_command(text) is None
