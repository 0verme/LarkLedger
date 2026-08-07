"""P07: deterministic confirmation directive parsing."""

from lark_ledger.entry_commands import (
    PendingDirective,
    try_parse_pending_directive,
)


def test_confirm_directive_variants() -> None:
    for text in (
        "确认 #C-A83F2",
        "确认 C-A83F2",
        "确认 #CA83F2",
        "确认 CA83F2",
        "同意 #c-a83f2",
        "执行 #C-A83F2",
        "  确认  #C-A83F2  ",
    ):
        assert try_parse_pending_directive(text) == PendingDirective(
            action="confirm", confirmation_code="CA83F2"
        ), text


def test_cancel_directive_variants() -> None:
    for text in (
        "取消 #C-A83F2",
        "放弃 C-A83F2",
        "取消 #ca83f2",
        "撤销 #C-A83F2",
        "撤销 C-A83F2",
    ):
        assert try_parse_pending_directive(text) == PendingDirective(
            action="cancel", confirmation_code="CA83F2"
        ), text


def test_list_directive() -> None:
    for text in ("待确认", "查看待确认", "确认列表"):
        assert try_parse_pending_directive(text) == PendingDirective(
            action="list"
        ), text


def test_invalid_confirmation_code_returns_error_text() -> None:
    result = try_parse_pending_directive("确认 #C-I83F2")
    assert isinstance(result, str)
    assert "格式无效" in result

    cancelled = try_parse_pending_directive("撤销 #C-I83F2")
    assert isinstance(cancelled, str)
    assert "格式无效" in cancelled


def test_bookkeeping_and_entry_messages_fall_through() -> None:
    # Ordinary bookkeeping and ledger-short-ID messages must not be intercepted.
    for text in (
        "午饭32元",
        "确认午饭32元",
        "确认",
        "删除 #A83F2",
        "取消删除 #A83F2",
        "查看 #A83F2",
        "把 #A83F2 改成35元",
        "恢复 #A83F2",
        "查看 #A83F2 之前的10笔",
        "",
    ):
        assert try_parse_pending_directive(text) is None, text


def test_empty_and_whitespace_fall_through() -> None:
    assert try_parse_pending_directive(None) is None  # type: ignore[arg-type]
    assert try_parse_pending_directive("   ") is None
