"""Deterministic parsing helpers for entry list/detail/mutation chat commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from lark_ledger.confirmation_id import (
    CONFIRMATION_PREFIX,
    normalize_confirmation_code,
)
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

# Confirmation directives (P07). Parsed deterministically BEFORE the AI
# interpreter so a 确认 #C-A83F2 message never becomes a bookkeeping attempt.
# The regex requires the literal ``C`` prefix (so "确认午饭32元" falls through),
# but captures a loose code and lets confirmation_id validate it — a "clearly a
# confirmation with a bad code" message returns an error instead of silently
# falling into bookkeeping.
_CONFIRM_VERBS = "确认|同意|执行"
_CANCEL_VERBS = "取消|放弃|撤销"

_PENDING_CONFIRM_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:{_CONFIRM_VERBS})\s*#?{CONFIRMATION_PREFIX}-?(?P<code>\S{{1,12}})$",
    re.IGNORECASE,
)
_PENDING_CANCEL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:{_CANCEL_VERBS})\s*#?{CONFIRMATION_PREFIX}-?(?P<code>\S{{1,12}})$",
    re.IGNORECASE,
)
_PENDING_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:查看)?\s*(?:待确认|确认列表)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PendingDirective:
    """A deterministic confirmation directive (确认/取消/查看待确认)."""

    action: Literal["confirm", "cancel", "list"]
    confirmation_code: str | None = None


def try_parse_pending_directive(text: str) -> PendingDirective | str | None:
    """Parse confirmation directives without AI.

    Returns:
    - ``PendingDirective`` on success (``confirmation_code`` is the storage form
      ``CA83F2``)
    - ``str`` user-facing error when the message is clearly a confirmation
      directive with an invalid code
    - ``None`` when the message should fall through to bookkeeping / AI
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    if _PENDING_LIST_RE.fullmatch(stripped) is not None:
        return PendingDirective(action="list")

    directives: list[tuple[re.Pattern[str], Literal["confirm", "cancel"]]] = [
        (_PENDING_CONFIRM_RE, "confirm"),
        (_PENDING_CANCEL_RE, "cancel"),
    ]
    for pattern, action in directives:
        match = pattern.fullmatch(stripped)
        if match is None:
            continue
        try:
            code = normalize_confirmation_code(
                f"{CONFIRMATION_PREFIX}-{match.group('code')}"
            )
        except ValueError:
            return "确认编号格式无效。请使用例如：确认 #C-A83F2"
        return PendingDirective(action=action, confirmation_code=code)
    return None


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
