"""Deterministic Feishu insight commands (P33 §32).

``洞察 / 财务洞察 / 本月洞察`` 走 MessageProcessor → InsightService 的确定性
路径；绝不经过 AI interpreter。AI 解释是可选项，默认回退到确定性 summary。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InsightCommandAction(StrEnum):
    LIST = "list"


@dataclass(frozen=True)
class InsightCommand:
    action: InsightCommandAction


_INSIGHT_KEYWORDS = frozenset({"洞察", "财务洞察", "本月洞察", "值得关注"})


def try_parse_insight_command(text: str) -> InsightCommand | None:
    value = " ".join((text or "").strip().split())
    if value in _INSIGHT_KEYWORDS:
        return InsightCommand(InsightCommandAction.LIST)
    return None
