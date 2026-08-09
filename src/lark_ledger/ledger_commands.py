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
}
_ONE_ARG = {
    "创建账本": LedgerCommandAction.CREATE,
    "切换账本": LedgerCommandAction.SELECT,
    "设为默认账本": LedgerCommandAction.SET_DEFAULT,
}


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
    return None
