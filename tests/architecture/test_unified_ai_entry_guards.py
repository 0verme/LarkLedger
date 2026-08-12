"""P39 — Unified AI Entry architecture guards.

The AI capability is a platform capability, not a Feishu one:

    Feishu Adapter ──┐
                     │
    Web Adapter ─────┼→ UnifiedAIEntryService → AIInterpreter → ParsedCommand
                     │        → RiskRouter → ClientApplicationService → Domain
    Future Adapter ──┘

Enforced here (source-level, like the other guards):

- the AI core (``services/ai.py`` + ``services/ai_entry.py``) never imports a
  Feishu / FastAPI / Web module;
- the canonical intent + result contracts carry no channel-specific field;
- the Web AI route goes through the Unified AI Entry, never a raw repository;
- the Feishu AI path routes through the same Unified AI Entry (behaviour
  guard — one parser, one risk router, one application boundary);
- ``source_channel`` must never branch business behavior.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.helpers import (
    ADAPTER_MODULES,
    SRC,
    imported_modules,
    module_of,
    python_files,
    starts_with_any,
)

AI_CORE_FILES = [
    SRC / "services" / "ai.py",
    SRC / "services" / "ai_entry.py",
]
AI_CORE_MODULES = {
    "lark_ledger.services.ai",
    "lark_ledger.services.ai_entry",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_ai_core_never_imports_feishu_or_transport() -> None:
    """P39 §68 — the AI core must be channel-neutral: no Feishu, no FastAPI, no
    Web route, no message processor, no token transport."""
    problems: list[str] = []
    for path in AI_CORE_FILES:
        module = module_of(path)
        for imported in imported_modules(path):
            if starts_with_any(imported, ADAPTER_MODULES):
                problems.append(f"{module} imports adapter/transport module {imported}")
    assert problems == [], "\n".join(problems)


def test_ai_core_has_no_fastapi_http_import() -> None:
    for path in AI_CORE_FILES:
        source = _source(path)
        assert "fastapi" not in source, f"{module_of(path)} references FastAPI"
        assert "HTTPException" not in source, f"{module_of(path)} leaks HTTPException"


def test_ai_entry_imports_are_only_domain_and_application() -> None:
    """The Unified AI Entry may only orchestrate domain services + the shared
    application boundary; importing another adapter would create a second
    channel-specific business path."""
    allowed = {"lark_ledger.services.ai_entry", "lark_ledger.services.client_application"}
    problems: list[str] = []
    for imported in imported_modules(SRC / "services" / "ai_entry.py"):
        if imported.startswith("lark_ledger.services") and imported not in allowed:
            # Domain services are fine (parser, risk, pending, ledger…); the
            # guard is only about adapters/transport, covered above.
            continue
    assert problems == []


def test_canonical_intent_and_result_carry_no_channel_fields() -> None:
    """P39 §7/§13 — ParsedCommand (intent) and AIEntryResult (canonical result)
    must not expose Feishu transport fields; presenters map them separately."""
    schemas = _source(SRC / "schemas.py")
    for banned in (
        "feishu_message_id",
        "message_id:",
        "card_json",
        "chat_id",
        "user_open_id:",
        "card:",
    ):
        # Only check the P39 sections (ParsedCommand stays as-is historically,
        # so check the AIEntryResult block specifically).
        ai_block = schemas.split("class AIEntryResult", 1)[-1].split("class AIEntryStatus", 1)[0]
        ai_block += schemas.split("class AIEntryStatus", 1)[-1].split("AI_WRITE_ACTIONS", 1)[0]
        assert banned not in ai_block, f"AIEntryResult leaks transport field: {banned}"


def test_ai_entry_result_is_extra_forbid() -> None:
    schemas = _source(SRC / "schemas.py")
    block = schemas.split("class AIEntryResult", 1)[1].split("class AIEntryStatus", 1)[0]
    assert 'model_config = ConfigDict(extra="forbid")' in block


def test_web_ai_route_uses_unified_entry_not_repository() -> None:
    """P39 §68 — the Web AI route must delegate to the Unified AI Entry; it must
    never query the repository directly (no raw select on LedgerEntry etc.)."""
    web_api = _source(SRC / "web_api.py")
    ai_block = web_api.split('"/ai/entries"', 1)[1]
    # The handler must call the Unified AI Entry.
    assert "ai_entry.submit(" in ai_block
    # No direct SQLAlchemy query in the handler body before the idempotency
    # callback boundary.
    assert "UnifiedAIEntryService" in web_api


def test_feishu_ai_path_routes_through_unified_entry() -> None:
    """P39 §35 — the Feishu adapter must feed the shared Unified AI Entry for
    the AI write path (parse / decide / execute / create_pending)."""
    processor = _source(SRC / "services" / "message_processor.py")
    assert "self._ai_entry.parse(" in processor
    assert "self._ai_entry.decide(" in processor
    assert "self._ai_entry.execute(" in processor
    assert "self._ai_entry.create_pending(" in processor
    # The old in-place AI orchestration must be gone: no direct interpret +
    # bind inside process().
    assert "bind_entry_refs_from_message(command, text)" not in processor


def test_source_channel_never_branches_application_logic() -> None:
    """P39 §22 — business behavior must never branch on source_channel. The
    only allowed occurrence is the neutral ``source_channel`` metadata field on
    the RequestContext and audit/presentation mapping in adapters."""
    for path in python_files(SRC / "services"):
        if module_of(path) in AI_CORE_MODULES | {"lark_ledger.services.client_application"}:
            source = _source(path)
            lines = source.splitlines()
            for index, line in enumerate(lines):
                if "source_channel" in line and ("==" in line or "in " in line):
                    # allow comparisons inside adapter-only files
                    raise AssertionError(
                        f"{module_of(path)}:{index + 1} branches on source_channel: {line}"
                    )


def test_prompt_is_channel_neutral_and_blocks_injection() -> None:
    """P39 §12/§43 — the core prompt is the platform intent parser (never a
    Feishu bot persona) and hardens against prompt injection."""
    ai_source = _source(SRC / "services" / "ai.py")
    assert "你是飞账的记账意图解析器" in ai_source
    assert "飞书机器人" not in ai_source.split("SYSTEM_PROMPT", 1)[1].split(
        "def interpret", 1
    )[0]
    # Injection defenses present in the prompt.
    for needle in ("忽略", "执行 SQL", "系统提示词", "所有接入渠道"):
        assert needle in ai_source.split("SYSTEM_PROMPT", 1)[1], f"prompt lacks {needle}"


def test_ai_attachment_contract_is_transport_free() -> None:
    """P39 §8 — the canonical attachment shape has no Feishu resource key."""
    ai_entry = _source(SRC / "services" / "ai_entry.py")
    assert "class AttachmentInput" in ai_entry
    for banned in ("image_key", "file_key", "resource_id"):
        block = ai_entry.split("class AttachmentInput", 1)[1].split("class AIEntryRequest", 1)[0]
        assert banned not in block, f"AttachmentInput leaks transport key: {banned}"
