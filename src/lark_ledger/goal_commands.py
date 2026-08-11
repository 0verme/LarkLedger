"""Deterministic Feishu goal commands (P33 §31).

第一版只做「查看」：列出当前账本的目标与确定性进度。创建 / 编辑在 Web
完成——不为聊天命令引入复杂 DSL。命令不经 AI interpreter。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GoalCommandAction(StrEnum):
    LIST = "list"


@dataclass(frozen=True)
class GoalCommand:
    action: GoalCommandAction


_GOAL_KEYWORDS = frozenset({"我的目标", "目标", "查看目标", "财务目标", "目标进度"})


def try_parse_goal_command(text: str) -> GoalCommand | None:
    """Parse 我的目标 / 目标 / 查看目标 without AI.

    Returns ``None`` when the message should fall through to other parsers
    (so it may still become an entry / recurring / overview command).
    """
    value = " ".join((text or "").strip().split())
    if value in _GOAL_KEYWORDS:
        return GoalCommand(GoalCommandAction.LIST)
    return None
