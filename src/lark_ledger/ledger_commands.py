from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LedgerCommandAction(StrEnum):
    LIST = "list"
    CREATE = "create"
    SELECT = "select"
    CURRENT = "current"
    SET_DEFAULT = "set_default"
    RENAME = "rename"


@dataclass(frozen=True)
class LedgerCommand:
    action: LedgerCommandAction
    name: str | None = None
    new_name: str | None = None


_NO_ARG = {
    "账本列表": LedgerCommandAction.LIST,
    "当前账本": LedgerCommandAction.CURRENT,
    "查看账本": LedgerCommandAction.LIST,
    "我的账本": LedgerCommandAction.LIST,
}
#: Canonical verb first, accepted synonyms after; ``_ONE_ARG`` verbs take a
#: trailing account name. The first matching prefix decides the command, so the
#: order matters only for display of the ``用法`` hint.
_ONE_ARG = {
    "创建账本": LedgerCommandAction.CREATE,
    "新建账本": LedgerCommandAction.CREATE,
    "创建新账本": LedgerCommandAction.CREATE,
    "切换账本": LedgerCommandAction.SELECT,
    "切换到账本": LedgerCommandAction.SELECT,
    "设为默认账本": LedgerCommandAction.SET_DEFAULT,
    "设置默认账本": LedgerCommandAction.SET_DEFAULT,
}

_LEDGER_USAGE = (
    "账本命令用法：\n"
    "· 创建账本 <名称>（或 新建账本 <名称>）\n"
    "· 切换账本 <名称>\n"
    "· 设为默认账本 <名称>\n"
    "· 重命名账本 <原名称> <新名称>\n"
    "· 账本列表 / 当前账本"
)

#: Prefixes that clearly signal ledger-management intent. When a message starts
#: with one of these but matches no exact command, reply with the ledger usage
#: hint instead of falling through to the generic bookkeeping help.
_LEDGER_PREFIXES = (
    "创建账本",
    "新建账本",
    "创建新账本",
    "切换账本",
    "切换到账本",
    "设为默认账本",
    "设置默认账本",
    "重命名账本",
    "账本列表",
    "当前账本",
    "查看账本",
    "我的账本",
    "账本",
)


def try_parse_ledger_command(text: str) -> LedgerCommand | str | None:
    value = " ".join(text.strip().split())
    if value in _NO_ARG:
        return LedgerCommand(_NO_ARG[value])
    for prefix, action in _ONE_ARG.items():
        marker = f"{prefix} "
        if value == prefix:
            return f"用法：{prefix} <账本名称>"
        if value.startswith(marker):
            return LedgerCommand(action, name=value[len(marker) :])
    if value == "重命名账本":
        return "用法：重命名账本 <原名称> <新名称>"
    if value.startswith("重命名账本 "):
        parts = value.split(" ")
        if len(parts) != 3:
            return "用法：重命名账本 <原名称> <新名称>（名称中请勿使用空格）"
        return LedgerCommand(LedgerCommandAction.RENAME, name=parts[1], new_name=parts[2])
    for prefix in _LEDGER_PREFIXES:
        if value.startswith(prefix):
            return _LEDGER_USAGE
    return None
