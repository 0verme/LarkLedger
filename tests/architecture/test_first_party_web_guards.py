"""P38 — First-party Web client architecture guards.

The browser client must stay channel-independent:

    Browser → UserSession → /api/web/v1 → ClientApplicationService → Domain

so the Web UI never smuggles in a second business implementation or a Feishu
dependency. These guards are source-level (like the Python layer guards):
they scan the shipped frontend sources for forbidden patterns.
"""

from __future__ import annotations

from pathlib import Path

WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"


def _ts_files() -> list[Path]:
    return sorted(p for p in WEB_SRC.rglob("*.ts") if "node_modules" not in str(p)) + sorted(
        p for p in WEB_SRC.rglob("*.tsx") if "node_modules" not in str(p)
    )


def test_web_client_never_imports_feishu_or_lark_sdk() -> None:
    """P38 §30 — Feishu OAuth is the identity provider, never a UI dependency.
    The first-party pages must not import any Feishu/Lark SDK or message
    contract."""
    banned_fragments = (
        "lark-oapi",
        "@larksuite",
        "feishu",
        "message_processor",
        "card_action",
        "websocket",
    )
    problems: list[str] = []
    for path in _ts_files():
        if path.name.endswith(".test.tsx") or path.name.endswith(".test.ts"):
            continue
        source = path.read_text(encoding="utf-8")
        for fragment in banned_fragments:
            if fragment.lower() in source.lower():
                # The word "飞书" in UI copy is fine; imports/code paths are not.
                if "import" in source[: max(1, source.find(fragment))] or "require(" in source:
                    problems.append(f"{path.relative_to(WEB_SRC)} mentions {fragment}")
    assert problems == [], "\n".join(problems)


def test_web_client_never_uses_unsafe_html_injection() -> None:
    """P38 §50 — no dangerouslySetInnerHTML anywhere in the client; user text
    (notes, account names, categories) is always rendered as text."""
    problems: list[str] = []
    for path in _ts_files():
        source = path.read_text(encoding="utf-8")
        if "dangerouslySetInnerHTML" in source:
            problems.append(str(path.relative_to(WEB_SRC)))
    assert problems == [], f"unsafe HTML injection: {problems}"


def test_web_client_uses_dialog_confirmation_not_window_confirm() -> None:
    """P38 §16 — delete/restore confirmation uses the in-app Dialog, never the
    browser-native window.confirm."""
    problems: list[str] = []
    for path in _ts_files():
        if path.name.endswith(".test.tsx") or path.name.endswith(".test.ts"):
            continue
        source = path.read_text(encoding="utf-8")
        if "window.confirm" in source:
            problems.append(str(path.relative_to(WEB_SRC)))
    assert problems == [], f"browser-native confirm: {problems}"


def test_web_client_amounts_stay_strings() -> None:
    """P38 §34 — amounts travel as decimal strings; no parseFloat-driven
    business arithmetic in the UI (the server owns Decimal math)."""
    problems: list[str] = []
    for path in _ts_files():
        if path.name.endswith(".test.tsx") or path.name.endswith(".test.ts"):
            continue
        source = path.read_text(encoding="utf-8")
        if "parseFloat" in source:
            problems.append(str(path.relative_to(WEB_SRC)))
    assert problems == [], f"float parsing in UI: {problems}"
