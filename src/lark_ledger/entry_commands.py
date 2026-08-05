"""Deterministic parsing helpers for entry list/detail/mutation chat commands."""

from __future__ import annotations

import re
from typing import Final

from lark_ledger.schemas import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    Action,
    ParsedCommand,
)
from lark_ledger.short_id import extract_entry_refs, normalize_entry_ref

# 查看 #A83F2 之前的10笔 / 查看 A83F2 之前的 10 笔
_BEFORE_PAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:查看|列出)?\s*"
    r"#?(?P<ref>[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{5})\s*"
    r"之前的\s*(?P<limit>\d{1,2})\s*笔",
    re.IGNORECASE,
)
# 查看 #A83F2 / 看看 a83f2 / 这笔 #A83F2 是什么
_GET_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:查看|看看|这笔)\s*"
    r"#?(?P<ref>[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{5})\s*"
    r"(?:是什么|详情)?$",
    re.IGNORECASE,
)
_DELETE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:删除|撤销|删掉)\s*#?(?P<ref>[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{5})\s*$",
    re.IGNORECASE,
)
_RESTORE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:恢复|找回|取消删除)\s*#?(?P<ref>[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{5})\s*$",
    re.IGNORECASE,
)

_MUTATION_ACTIONS = frozenset(
    {Action.UPDATE_ENTRY, Action.DELETE_ENTRY, Action.RESTORE_ENTRY, Action.GET_ENTRY}
)


def try_parse_deterministic_entry_command(text: str) -> ParsedCommand | str | None:
    """Parse stable short-ID list/detail/mutation commands without AI.

    Returns:
    - ParsedCommand on success
    - str user-facing error when the message is clearly a short-ID query but invalid
    - None when the message should fall through to the AI interpreter
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    before = _BEFORE_PAGE_RE.search(stripped)
    if before is not None:
        try:
            ref = normalize_entry_ref(before.group("ref"))
        except Exception:
            return "分页短 ID 格式无效。请使用五位编号，例如：查看 #A83F2 之前的10笔"
        limit = int(before.group("limit"))
        if limit < 1:
            limit = DEFAULT_LIST_LIMIT
        return ParsedCommand(
            action=Action.LIST_ENTRIES,
            before_entry_ref=ref,
            limit=min(limit, MAX_LIST_LIMIT),
        )

    delete_match = _DELETE_RE.fullmatch(stripped)
    if delete_match is not None:
        # "撤销刚才那笔" is last-command style, not short-ID delete.
        if "刚才" in stripped or "上一笔" in stripped or "最近一笔" in stripped:
            return None
        try:
            ref = normalize_entry_ref(delete_match.group("ref"))
        except Exception:
            return "短 ID 格式无效。请使用五位编号，例如：删除 #A83F2"
        return ParsedCommand(action=Action.DELETE_ENTRY, entry_ref=ref)

    restore_match = _RESTORE_RE.fullmatch(stripped)
    if restore_match is not None:
        try:
            ref = normalize_entry_ref(restore_match.group("ref"))
        except Exception:
            return "短 ID 格式无效。请使用五位编号，例如：恢复 #A83F2"
        return ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref=ref)

    get_match = _GET_RE.fullmatch(stripped)
    if get_match is not None:
        try:
            ref = normalize_entry_ref(get_match.group("ref"))
        except Exception:
            return "短 ID 格式无效。请使用五位编号，例如：查看 #A83F2"
        return ParsedCommand(action=Action.GET_ENTRY, entry_ref=ref)

    refs = extract_entry_refs(stripped)
    mutation_tokens = ("查看", "看看", "详情", "之前", "删除", "撤销", "恢复", "修改", "改成")
    if len(refs) > 1 and any(token in stripped for token in mutation_tokens):
        return "一次只能操作一个短 ID。请只发送一个引用，例如：删除 #A83F2"
    return None


def bind_entry_refs_from_message(
    command: ParsedCommand, text: str
) -> ParsedCommand | str:
    """Force short-ID fields to values present in the user message.

    Prevents the model from inventing or swapping entry references.
    """
    refs = extract_entry_refs(text)
    if command.action in _MUTATION_ACTIONS or command.action is Action.GET_ENTRY:
        if len(refs) > 1:
            return "一次只能操作一个短 ID。请只发送一个引用，例如：删除 #A83F2"
        if len(refs) == 1:
            return command.model_copy(update={"entry_ref": refs[0]})
        if command.entry_ref:
            return "消息中没有有效的短 ID。请写明五位编号，例如：把 #A83F2 改成35元"
        return command

    if command.action is Action.LIST_ENTRIES and command.before_entry_ref:
        try:
            requested = normalize_entry_ref(command.before_entry_ref)
        except Exception:
            return "分页短 ID 格式无效。请使用五位编号，例如：查看 #A83F2 之前的10笔"
        if requested in refs:
            return command.model_copy(update={"before_entry_ref": requested})
        if len(refs) == 1:
            return command.model_copy(update={"before_entry_ref": refs[0]})
        if not refs:
            return "分页需要消息中的短 ID。请使用：查看 #A83F2 之前的10笔"
        return "分页短 ID 必须来自消息内容，且一次只能指定一个。"

    return command
