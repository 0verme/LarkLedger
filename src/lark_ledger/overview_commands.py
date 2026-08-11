"""Deterministic household-overview commands (P31)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OverviewCommandAction(StrEnum):
    OVERVIEW = "overview"


@dataclass(frozen=True)
class OverviewCommand:
    action: OverviewCommandAction


_NO_ARG = frozenset({"概览", "家庭概览", "家庭开销", "本月概览"})


def try_parse_overview_command(text: str) -> OverviewCommand | None:
    """Parse 概览 / 家庭概览 / 家庭开销 without AI.

    Returns ``None`` when the message should fall through to other parsers.
    """
    value = " ".join((text or "").strip().split())
    if value in _NO_ARG:
        return OverviewCommand(OverviewCommandAction.OVERVIEW)
    return None
