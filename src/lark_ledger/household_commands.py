from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HouseholdCommandAction(StrEnum):
    CREATE = "create"
    LIST = "list"
    CURRENT = "current"
    MEMBERS = "members"
    INVITE = "invite"
    INVITATIONS = "invitations"
    ACCEPT = "accept"
    REJECT = "reject"
    SELECT_LEDGER = "select_ledger"
    LEAVE = "leave"


@dataclass(frozen=True)
class HouseholdCommand:
    action: HouseholdCommandAction
    argument: str | None = None


_NO_ARG = {
    "家庭列表": HouseholdCommandAction.LIST,
    "当前家庭": HouseholdCommandAction.CURRENT,
    "家庭成员": HouseholdCommandAction.MEMBERS,
    "家庭邀请列表": HouseholdCommandAction.INVITATIONS,
}

_ONE_ARG = {
    "创建家庭": (HouseholdCommandAction.CREATE, "<家庭名称>"),
    "邀请家庭成员": (HouseholdCommandAction.INVITE, "<飞书 open_id，例如 ou_xxx>"),
    "接受家庭邀请": (HouseholdCommandAction.ACCEPT, "<邀请编号>"),
    "拒绝家庭邀请": (HouseholdCommandAction.REJECT, "<邀请编号>"),
    "切换家庭账本": (HouseholdCommandAction.SELECT_LEDGER, "<家庭名称>"),
    "退出家庭": (HouseholdCommandAction.LEAVE, "<家庭名称>"),
}


def try_parse_household_command(text: str) -> HouseholdCommand | str | None:
    value = " ".join(text.strip().split())
    if value in _NO_ARG:
        return HouseholdCommand(_NO_ARG[value])
    for prefix, (action, usage) in _ONE_ARG.items():
        if value == prefix:
            return f"用法：{prefix} {usage}"
        marker = f"{prefix} "
        if value.startswith(marker):
            argument = value[len(marker) :].strip()
            if action is HouseholdCommandAction.INVITE and not argument.startswith("ou_"):
                return "邀请目标必须是唯一、完整的飞书 open_id。用法：邀请家庭成员 ou_xxx"
            return HouseholdCommand(action, argument)
    return None
