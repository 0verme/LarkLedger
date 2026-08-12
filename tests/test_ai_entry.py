"""P39 — Unified AI Entry test matrix.

Covers the parser contract (AI01–AI08), execution (EX01–EX12), channel
equivalence (C01–C08) and confirmation equivalence. The real LLM provider is
never called: a deterministic stub interpreter stands in for
``AIInterpreter``, so the tests exercise the pipeline contract — intent
parsing, risk routing, execution through ``ClientApplicationService`` and the
canonical ``AIEntryResult`` envelope — not model behaviour.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.models import (
    AccountType,
    AccountVisibility,
    Base,
    Direction,
    LedgerEntry,
    PendingCommand,
    PendingStatus,
)
from lark_ledger.schemas import (
    Action,
    AIEntryResult,
    AIEntryStatus,
    ParsedCommand,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.ai import CommandInterpretationError
from lark_ledger.services.ai_entry import AIEntryRequest, UnifiedAIEntryService
from lark_ledger.services.events import EventService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.message_processor import MessageProcessor


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        lark_app_id="app_id",
        lark_app_secret="app_secret",
        currency="CNY",
        timezone="Asia/Shanghai",
        pending_enabled=True,
    )


class StubInterpreter:
    """Deterministic stand-in for the AI intent parser (never calls a
    provider, never touches the database)."""

    def __init__(
        self,
        command: ParsedCommand | None = None,
        error: Exception | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.command = command
        self.error = error
        self.calls = calls

    @property
    def vision_configured(self) -> bool:
        return False

    @property
    def transcription_configured(self) -> bool:
        return False

    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes] | None = None
    ) -> ParsedCommand:
        if self.calls is not None:
            self.calls.append(text)
        if self.error is not None:
            raise self.error
        if self.command is None:
            raise AssertionError("StubInterpreter needs a command")
        return self.command


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, message_id: str, text: str, *, uuid: str | None = None) -> None:
        del uuid
        self.texts.append(text)


def text_event(text: str, message_id: str, open_id: str = "ou_user") -> dict[str, object]:
    return {
        "event_id": message_id,
        "sender": {"sender_id": {"open_id": open_id}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    }


def _command(
    action: Action,
    *,
    amount: str | None = None,
    direction: Direction | None = None,
    category: str | None = None,
    note: str | None = None,
    occurred_at: datetime | None = None,
    account_hint: str | None = None,
    entry_ref: str | None = None,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    limit: int | None = None,
    from_account_hint: str | None = None,
    to_account_hint: str | None = None,
) -> ParsedCommand:
    return ParsedCommand(
        action=action,
        amount=Decimal(amount) if amount is not None else None,
        direction=direction,
        category=category,
        note=note,
        occurred_at=(
            occurred_at
            if occurred_at is not None
            else (
                datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
                if action in {Action.CREATE, Action.TRANSFER}
                else None
            )
        ),
        account_hint=account_hint,
        entry_ref=entry_ref,
        range_start=range_start,
        range_end=range_end,
        limit=limit,
        from_account_hint=from_account_hint,
        to_account_hint=to_account_hint,
    )


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _identity(factory: async_sessionmaker[AsyncSession], open_id: str) -> RequestContext:
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id=open_id)
        await session.commit()
        return context


def _request(context: RequestContext, text: str, request_id: str = "ai:test") -> AIEntryRequest:
    return AIEntryRequest(
        context=context,
        text=text,
        request_id=request_id,
        source_message_ref=request_id,
    )


async def _submit(
    factory: async_sessionmaker[AsyncSession],
    interpreter: StubInterpreter,
    context: RequestContext,
    text: str,
    *,
    request_id: str = "ai:test",
    now: datetime | None = None,
    pending_enabled: bool = True,
):
    settings = _settings()
    if not pending_enabled:
        settings = settings.model_copy(update={"pending_enabled": False})
    service = UnifiedAIEntryService(settings, factory, interpreter=interpreter)
    # ``submit`` is the Web adapter path: the same actor/ledger enters through
    # the neutral ``web`` channel (identity is already resolved — no new
    # bootstrap happens here, so Feishu/Web equivalence stays on one User).
    web_context = RequestContext(
        actor_user_id=context.actor_user_id,
        ledger_id=context.ledger_id,
        source_channel="web",
        external_subject_id=context.external_subject_id,
        actor_kind="user",
    )
    async with factory() as session:
        outcome = await service.submit(
            session=session,
            request=_request(web_context, text, request_id),
            commit_changes=True,
        )
        return outcome


async def _entry_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(LedgerEntry)) or 0)


async def _latest_entry(factory: async_sessionmaker[AsyncSession]) -> LedgerEntry:
    async with factory() as session:
        return (
            (await session.execute(select(LedgerEntry).order_by(LedgerEntry.created_at.desc())))
            .scalars()
            .first()
        )


# ---------------------------------------------------------------------------
# Parser contract (AI01–AI08)
# ---------------------------------------------------------------------------


async def test_ai01_lunch_expense_parses_to_expense_28(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ai01")
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
        ctx,
        "午饭28",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.operation == "create"
    assert outcome.amount == "28.00"
    assert outcome.direction == "expense"
    assert outcome.category == "餐饮"
    assert await _entry_count(factory) == 1


async def test_ai02_salary_parses_to_income(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ai02")
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="18000.00",
                direction=Direction.INCOME,
                category="工资",
            )
        ),
        ctx,
        "工资18000",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.direction == "income"
    assert outcome.amount == "18000.00"


async def test_ai03_yesterday_taxi_keeps_occurred_at(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    occurred = datetime(2026, 8, 13, 8, 30, tzinfo=UTC)
    ctx = await _identity(factory, "ou_ai03")
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="35.00",
                direction=Direction.EXPENSE,
                category="交通",
                note="打车",
                occurred_at=occurred,
            )
        ),
        ctx,
        "昨天打车35",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.occurred_at == occurred


async def test_ai04_account_hint_is_carried_and_not_trusted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ai04")
    async with factory() as session:
        await AccountService(session).create(
            ctx, name="招行", account_type=AccountType.ASSET, currency="CNY"
        )
        await session.commit()
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="32.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="星巴克",
                account_hint="招行",
            )
        ),
        ctx,
        "星巴克32，用招行",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.account == "招行"


async def test_ai05_malformed_model_output_is_rejected_without_mutation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ai05")
    outcome = await _submit(
        factory,
        StubInterpreter(error=CommandInterpretationError("invalid")),
        ctx,
        "午饭28",
    )
    assert outcome.status is AIEntryStatus.CLARIFICATION_REQUIRED
    assert await _entry_count(factory) == 0


async def test_ai06_invalid_amount_cannot_build_intent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ai06")
    # Negative amount and 3-decimal amounts are rejected by the schema before
    # any execution (P39 §44/§45).
    outcome = await _submit(
        factory,
        StubInterpreter(error=CommandInterpretationError("negative amount")),
        ctx,
        "午饭-5",
    )
    assert outcome.status is AIEntryStatus.CLARIFICATION_REQUIRED
    assert await _entry_count(factory) == 0

    with pytest.raises(ValidationError):
        _command(Action.CREATE, amount="-5.00", direction=Direction.EXPENSE, category="其他")
    with pytest.raises(ValidationError):
        _command(Action.CREATE, amount="0.001", direction=Direction.EXPENSE, category="其他")
    # Decimal-safe amounts survive the round trip.
    for raw in ("0.1", "28.88", "10000.01"):
        parsed = _command(Action.CREATE, amount=raw, direction=Direction.EXPENSE, category="其他")
        assert parsed.amount == Decimal(raw)


async def test_ai07_help_action_becomes_clarification(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ai07")
    outcome = await _submit(
        factory,
        StubInterpreter(_command(Action.HELP)),
        ctx,
        "记一笔 28",
    )
    assert outcome.status is AIEntryStatus.CLARIFICATION_REQUIRED
    assert "我可以帮你记账" in outcome.message
    assert await _entry_count(factory) == 0


async def test_ai08_private_account_is_not_resolvable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_p_owner", display_name="甲"
        )
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_p_member", display_name="乙"
        )
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_p_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        ledger_id = home.ledger.id
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=ledger_id,
            source_channel="web",
            external_subject_id="ou_p_owner",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=ledger_id,
            source_channel="web",
            external_subject_id="ou_p_member",
        )
        account = await AccountService(session).create(
            owner_ctx, name="私房钱", account_type=AccountType.CASH, currency="CNY"
        )
        await AccountService(session).set_visibility(
            owner_ctx, account.id, visibility=AccountVisibility.PRIVATE
        )
        await session.commit()

    # B (member) asks to use A's private account by name: resolution fails and
    # nothing leaks that the account exists (P39 §49).
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="100.00",
                direction=Direction.EXPENSE,
                category="其他",
                account_hint="私房钱",
            )
        ),
        member_ctx,
        "用A的私人卡记100",
    )
    assert outcome.status is AIEntryStatus.ERROR
    assert "私房钱" not in outcome.message
    assert await _entry_count(factory) == 0


# ---------------------------------------------------------------------------
# Execution (EX01–EX12)
# ---------------------------------------------------------------------------


async def test_ex01_create_expense(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex01")
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
        ctx,
        "午饭28",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.resource_id is not None
    row = await _latest_entry(factory)
    assert row is not None
    assert row.amount == Decimal("28.00")
    assert row.direction is Direction.EXPENSE
    assert row.category == "餐饮"
    assert row.source_type == "web"
    assert str(row.created_by_user_id) == str(ctx.actor_user_id)


async def test_ex02_create_income(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex02")
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="18000.00",
                direction=Direction.INCOME,
                category="工资",
            )
        ),
        ctx,
        "工资18000",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    row = await _latest_entry(factory)
    assert row.amount == Decimal("18000.00")
    assert row.direction is Direction.INCOME


async def test_ex03_update_last(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex03")
    await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
        ctx,
        "午饭28",
    )
    outcome = await _submit(
        factory,
        StubInterpreter(_command(Action.UPDATE_LAST, amount="30.00")),
        ctx,
        "改成30",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.amount == "30.00"
    row = await _latest_entry(factory)
    assert row.amount == Decimal("30.00")
    assert await _entry_count(factory) == 1


async def test_ex04_delete_last(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex04")
    await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
        ctx,
        "午饭28",
    )
    outcome = await _submit(
        factory, StubInterpreter(_command(Action.UNDO_LAST)), ctx, "撤销刚才那笔"
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert outcome.operation == "undo_last"
    row = await _latest_entry(factory)
    assert row.deleted_at is not None


async def test_ex05_restore_entry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex05")
    await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
        ctx,
        "午饭28",
    )
    row = await _latest_entry(factory)
    short_id = row.short_id
    await _submit(
        factory,
        StubInterpreter(_command(Action.DELETE_ENTRY, entry_ref=short_id)),
        ctx,
        f"删除 #{short_id}",
    )
    assert (await _latest_entry(factory)).deleted_at is not None
    outcome = await _submit(
        factory,
        StubInterpreter(_command(Action.RESTORE_ENTRY, entry_ref=short_id)),
        ctx,
        f"恢复 #{short_id}",
    )
    assert outcome.status is AIEntryStatus.EXECUTED
    assert (await _latest_entry(factory)).deleted_at is None


async def test_ex06_transfer_requires_confirmation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex06")
    async with factory() as session:
        await AccountService(session).create(
            ctx, name="招行", account_type=AccountType.ASSET, currency="CNY"
        )
        await AccountService(session).create(
            ctx, name="支付宝", account_type=AccountType.ASSET, currency="CNY"
        )
        await session.commit()
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.TRANSFER,
                amount="1000.00",
                from_account_hint="招行",
                to_account_hint="支付宝",
            )
        ),
        ctx,
        "从招行转1000到支付宝",
    )
    assert outcome.status is AIEntryStatus.CONFIRMATION_REQUIRED
    assert outcome.pending_command_id is not None
    assert outcome.risk == "transfer"
    async with factory() as session:
        pending = await session.scalar(
            select(PendingCommand).where(PendingCommand.actor_user_id == ctx.actor_user_id)
        )
        assert pending is not None
        assert pending.status == PendingStatus.PENDING.value
        assert pending.transport == "web"
    # No ledger mutation until the user confirms.
    assert await _entry_count(factory) == 0


async def test_ex07_query_returns_query_result(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex07")
    await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
        ctx,
        "午饭28",
    )
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(Action.LIST_ENTRIES, limit=5),
        ),
        ctx,
        "查看最近5笔",
    )
    assert outcome.status is AIEntryStatus.QUERY_RESULT
    assert outcome.operation == "list_entries"
    assert "最近" in outcome.message or "1" in outcome.message


async def test_ex08_unauthorized_ledger_is_an_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _identity(factory, "ou_ex08_owner")
    outsider = await _identity(factory, "ou_ex08_outsider")
    # The outsider's context targets the owner's personal ledger directly —
    # authorization must reject it (P39 §20/§50).
    forged = RequestContext(
        actor_user_id=outsider.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="web",
        external_subject_id="ou_ex08_outsider",
    )
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
        forged,
        "午饭28",
    )
    assert outcome.status is AIEntryStatus.ERROR
    assert "账本不可访问" in outcome.message
    assert await _entry_count(factory) == 0


async def test_ex09_private_isolation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_ex09_owner", display_name="甲"
        )
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_ex09_member", display_name="乙"
        )
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_ex09_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="web",
            external_subject_id="ou_ex09_owner",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="web",
            external_subject_id="ou_ex09_member",
        )
        account = await AccountService(session).create(
            owner_ctx, name="私密卡", account_type=AccountType.ASSET, currency="CNY"
        )
        await AccountService(session).set_visibility(
            owner_ctx, account.id, visibility=AccountVisibility.PRIVATE
        )
        await session.commit()

    # Owner records to the private account; member records to the shared default.
    owner_outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="100.00",
                direction=Direction.EXPENSE,
                category="其他",
                account_hint="私密卡",
            )
        ),
        owner_ctx,
        "私密卡100",
    )
    assert owner_outcome.status is AIEntryStatus.EXECUTED
    member_outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="100.00",
                direction=Direction.EXPENSE,
                category="其他",
                account_hint="私密卡",
            )
        ),
        member_ctx,
        "用私密卡100",
    )
    assert member_outcome.status is AIEntryStatus.ERROR
    assert await _entry_count(factory) == 1


async def test_ex10_cross_ledger_cannot_leak(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _identity(
        factory,
        "ou_ex10_owner",
    )
    member = await _identity(factory, "ou_ex10_member")
    # member tries to write into owner's personal ledger via a guessed id.
    forged = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=owner.ledger_id,
        source_channel="web",
        external_subject_id="ou_ex10_member",
    )
    outcome = await _submit(
        factory,
        StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
        forged,
        "午饭28",
    )
    assert outcome.status is AIEntryStatus.ERROR
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.ledger_id == owner.ledger_id)
        )
        assert int(count or 0) == 0


async def test_ex11_duplicate_replay_is_web_layer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Idempotent replay of the same Idempotency-Key is exercised at the Web API
    # layer (tests/test_web_ai_entry.py) through ClientIdempotencyService; here
    # we just assert the pipeline has no hidden global dedup.
    ctx = await _identity(factory, "ou_ex11")
    interpreter = StubInterpreter(
        _command(
            Action.CREATE,
            amount="28.00",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="午饭",
        )
    )
    first = await _submit(
        factory, interpreter, ctx, "午饭28", request_id="ai:dup-1", pending_enabled=False
    )
    second = await _submit(
        factory, interpreter, ctx, "午饭28", request_id="ai:dup-2", pending_enabled=False
    )
    assert first.status is AIEntryStatus.EXECUTED
    assert second.status is AIEntryStatus.EXECUTED
    assert first.replayed is False and second.replayed is False
    # Both submissions are independent requests (the idempotency layer lives in
    # the adapter); exactly-once for a single logical request is Web-layer.
    assert await _entry_count(factory) == 2


async def test_ex12_provider_timeout_produces_no_mutation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_ex12")
    outcome = await _submit(
        factory,
        StubInterpreter(error=TimeoutError("provider timed out")),
        ctx,
        "午饭28",
    )
    assert outcome.status is AIEntryStatus.ERROR
    assert "AI 服务暂时不可用" in outcome.message
    assert await _entry_count(factory) == 0


# ---------------------------------------------------------------------------
# Channel equivalence (C01–C08): same user/ledger/input, Feishu vs Web
# ---------------------------------------------------------------------------


async def _feishu_process(
    factory: async_sessionmaker[AsyncSession],
    interpreter: StubInterpreter,
    event: dict[str, object],
    *,
    pending_enabled: bool = True,
) -> list[str]:
    settings = _settings()
    if not pending_enabled:
        settings = settings.model_copy(update={"pending_enabled": False})
    feishu = RecordingFeishu()
    processor = MessageProcessor(settings, factory, feishu, interpreter)
    service = EventService(factory, processor, worker_enabled=False)
    await service.handle_safely(str(event["event_id"]), event, transport="webhook")
    return feishu.texts


async def _web_submit(
    factory: async_sessionmaker[AsyncSession],
    interpreter: StubInterpreter,
    open_id: str,
    text: str,
    request_id: str,
    *,
    pending_enabled: bool = True,
) -> AIEntryResult:
    ctx = await _identity(factory, open_id)
    return await _submit(
        factory,
        interpreter,
        ctx,
        text,
        request_id=request_id,
        pending_enabled=pending_enabled,
    )


async def test_c01_lunch_equivalent_across_channels(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    command = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
    )
    await _feishu_process(
        factory,
        StubInterpreter(command),
        text_event("午饭28", "om_c01", "ou_c01"),
        pending_enabled=False,
    )
    async with factory() as session:
        feishu_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "om_c01")
                )
            )
            .scalars()
            .first()
        )
    await _web_submit(
        factory, StubInterpreter(command), "ou_c01", "午饭28", "ai:c01", pending_enabled=False
    )
    web_row = await _latest_entry(factory)
    assert feishu_row is not None and web_row is not None
    for field in (
        "amount",
        "direction",
        "category",
        "note",
        "occurred_at",
        "ledger_id",
        "currency",
    ):
        assert getattr(web_row, field) == getattr(feishu_row, field), field
    assert str(web_row.created_by_user_id) == str(feishu_row.created_by_user_id)


async def test_c02_income_equivalent_across_channels(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    command = _command(
        Action.CREATE,
        amount="18000.00",
        direction=Direction.INCOME,
        category="工资",
        note="工资",
    )
    await _feishu_process(
        factory,
        StubInterpreter(command),
        text_event("工资18000", "om_c02", "ou_c02"),
        pending_enabled=False,
    )
    await _web_submit(
        factory, StubInterpreter(command), "ou_c02", "工资18000", "ai:c02", pending_enabled=False
    )
    async with factory() as session:
        feishu_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "om_c02")
                )
            )
            .scalars()
            .first()
        )
        web_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "ai:c02")
                )
            )
            .scalars()
            .first()
        )
    assert feishu_row is not None and web_row is not None
    assert feishu_row.amount == web_row.amount == Decimal("18000.00")
    assert feishu_row.direction is web_row.direction is Direction.INCOME
    assert feishu_row.category == web_row.category == "工资"


async def test_c03_yesterday_taxi_date_equivalent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    occurred = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    command = _command(
        Action.CREATE,
        amount="35.00",
        direction=Direction.EXPENSE,
        category="交通",
        note="打车",
        occurred_at=occurred,
    )
    await _feishu_process(
        factory,
        StubInterpreter(command),
        text_event("昨天打车35", "om_c03", "ou_c03"),
        pending_enabled=False,
    )
    await _web_submit(
        factory, StubInterpreter(command), "ou_c03", "昨天打车35", "ai:c03", pending_enabled=False
    )
    async with factory() as session:
        feishu_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "om_c03")
                )
            )
            .scalars()
            .first()
        )
        web_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "ai:c03")
                )
            )
            .scalars()
            .first()
        )
    assert feishu_row is not None and web_row is not None
    # SQLite DateTime columns come back naive; normalize for comparison.
    assert (
        feishu_row.occurred_at.replace(tzinfo=UTC)
        == web_row.occurred_at.replace(tzinfo=UTC)
        == occurred
    )


async def test_c04_update_last_equivalent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Distinct occurred_at keeps "上一笔" deterministic (occurred_at desc) so
    # each channel's update_last targets its own freshly created row.
    create_feishu = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
    )
    create_web = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
    )
    update = _command(Action.UPDATE_LAST, amount="30.00")
    # Feishu: create then amend
    await _feishu_process(
        factory,
        StubInterpreter(create_feishu),
        text_event("午饭28", "om_c04a", "ou_c04"),
        pending_enabled=False,
    )
    await _feishu_process(
        factory,
        StubInterpreter(update),
        text_event("改成30", "om_c04b", "ou_c04"),
        pending_enabled=False,
    )
    # Web: create then amend (fresh user, same ledger semantics)
    await _web_submit(
        factory, StubInterpreter(create_web), "ou_c04", "午饭28", "ai:c04a", pending_enabled=False
    )
    await _web_submit(
        factory, StubInterpreter(update), "ou_c04", "改成30", "ai:c04b", pending_enabled=False
    )
    async with factory() as session:
        feishu_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "om_c04a")
                )
            )
            .scalars()
            .first()
        )
        web_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "ai:c04a")
                )
            )
            .scalars()
            .first()
        )
    # "上一笔" resolves identically on both channels (actor + ledger ordering).
    assert feishu_row.amount == web_row.amount == Decimal("30.00")


async def test_c05_undo_last_equivalent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    create = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
    )
    undo = _command(Action.UNDO_LAST)
    await _feishu_process(
        factory,
        StubInterpreter(create),
        text_event("午饭28", "om_c05a", "ou_c05"),
        pending_enabled=False,
    )
    await _feishu_process(
        factory,
        StubInterpreter(undo),
        text_event("撤销刚才那笔", "om_c05b", "ou_c05"),
        pending_enabled=False,
    )
    await _web_submit(
        factory, StubInterpreter(create), "ou_c05", "午饭28", "ai:c05a", pending_enabled=False
    )
    await _web_submit(
        factory, StubInterpreter(undo), "ou_c05", "撤销刚才那笔", "ai:c05b", pending_enabled=False
    )
    async with factory() as session:
        feishu_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "om_c05a")
                )
            )
            .scalars()
            .first()
        )
        web_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "ai:c05a")
                )
            )
            .scalars()
            .first()
        )
    assert feishu_row.deleted_at is not None
    assert web_row.deleted_at is not None


async def test_c06_restore_equivalent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    create = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
    )
    await _feishu_process(
        factory,
        StubInterpreter(create),
        text_event("午饭28", "om_c06a", "ou_c06"),
        pending_enabled=False,
    )
    await _web_submit(
        factory, StubInterpreter(create), "ou_c06", "午饭28", "ai:c06a", pending_enabled=False
    )
    async with factory() as session:
        feishu_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "om_c06a")
                )
            )
            .scalars()
            .first()
        )
        web_row = (
            (
                await session.execute(
                    select(LedgerEntry).where(LedgerEntry.source_message_id == "ai:c06a")
                )
            )
            .scalars()
            .first()
        )
    delete_f = _command(Action.DELETE_ENTRY, entry_ref=feishu_row.short_id)
    restore_f = _command(Action.RESTORE_ENTRY, entry_ref=feishu_row.short_id)
    delete_w = _command(Action.DELETE_ENTRY, entry_ref=web_row.short_id)
    restore_w = _command(Action.RESTORE_ENTRY, entry_ref=web_row.short_id)
    await _feishu_process(
        factory,
        StubInterpreter(delete_f),
        text_event(f"删除 #{feishu_row.short_id}", "om_c06b", "ou_c06"),
        pending_enabled=False,
    )
    await _feishu_process(
        factory,
        StubInterpreter(restore_f),
        text_event(f"恢复 #{feishu_row.short_id}", "om_c06c", "ou_c06"),
        pending_enabled=False,
    )
    await _web_submit(
        factory,
        StubInterpreter(delete_w),
        "ou_c06",
        f"删除 #{web_row.short_id}",
        "ai:c06b",
        pending_enabled=False,
    )
    await _web_submit(
        factory,
        StubInterpreter(restore_w),
        "ou_c06",
        f"恢复 #{web_row.short_id}",
        "ai:c06c",
        pending_enabled=False,
    )
    async with factory() as session:
        feishu_row = await session.get(LedgerEntry, feishu_row.id)
        web_row = await session.get(LedgerEntry, web_row.id)
    assert feishu_row.deleted_at is None and web_row.deleted_at is None


async def test_c07_transfer_confirmation_equivalent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_c07")
    async with factory() as session:
        await AccountService(session).create(
            ctx, name="招行", account_type=AccountType.ASSET, currency="CNY"
        )
        await AccountService(session).create(
            ctx, name="支付宝", account_type=AccountType.ASSET, currency="CNY"
        )
        await session.commit()
    transfer = _command(
        Action.TRANSFER,
        amount="1000.00",
        from_account_hint="招行",
        to_account_hint="支付宝",
    )
    await _feishu_process(
        factory, StubInterpreter(transfer), text_event("从招行转1000到支付宝", "om_c07", "ou_c07")
    )
    outcome = await _web_submit(
        factory, StubInterpreter(transfer), "ou_c07", "从招行转1000到支付宝", "ai:c07"
    )
    assert outcome.status is AIEntryStatus.CONFIRMATION_REQUIRED
    async with factory() as session:
        rows = (await session.execute(select(PendingCommand))).scalars().all()
        assert len(rows) == 2
        by_transport = {row.transport: row for row in rows}
        assert by_transport["feishu"] is not None
        assert by_transport["web"] is not None
        assert by_transport["feishu"].payload_json == by_transport["web"].payload_json
        assert by_transport["feishu"].ledger_id == by_transport["web"].ledger_id == ctx.ledger_id


async def test_c08_query_equivalent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    create = _command(
        Action.CREATE,
        amount="28.00",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
    )
    query = _command(Action.LIST_ENTRIES, limit=5)
    await _feishu_process(
        factory,
        StubInterpreter(create),
        text_event("午饭28", "om_c08a", "ou_c08"),
        pending_enabled=False,
    )
    feishu_texts = await _feishu_process(
        factory,
        StubInterpreter(query),
        text_event("查看最近5笔", "om_c08b", "ou_c08"),
        pending_enabled=False,
    )
    outcome = await _web_submit(
        factory, StubInterpreter(query), "ou_c08", "查看最近5笔", "ai:c08", pending_enabled=False
    )
    assert outcome.status is AIEntryStatus.QUERY_RESULT
    assert feishu_texts, "Feishu must reply to the query"
    assert "午饭" in " ".join(feishu_texts) or "28" in " ".join(feishu_texts)
    # Query writes nothing on either channel.
    assert await _entry_count(factory) == 1


# ---------------------------------------------------------------------------
# Confirmation equivalence: one semantic operation, same pending contract
# ---------------------------------------------------------------------------


async def test_confirmation_equivalent_semantics_across_channels(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _identity(factory, "ou_conf")
    async with factory() as session:
        await AccountService(session).create(
            ctx, name="现金", account_type=AccountType.ASSET, currency="CNY"
        )
        await AccountService(session).create(
            ctx, name="储蓄", account_type=AccountType.ASSET, currency="CNY"
        )
        await session.commit()
    transfer = _command(
        Action.TRANSFER,
        amount="500.00",
        from_account_hint="现金",
        to_account_hint="储蓄",
    )
    # Both channels freeze the SAME frozen ParsedCommand into a pending row —
    # neither executes without confirmation (P39 §74).
    await _feishu_process(
        factory, StubInterpreter(transfer), text_event("从现金转500到储蓄", "om_conf_f", "ou_conf")
    )
    web_outcome = await _web_submit(
        factory, StubInterpreter(transfer), "ou_conf", "从现金转500到储蓄", "ai:conf_w"
    )
    assert web_outcome.status is AIEntryStatus.CONFIRMATION_REQUIRED
    assert web_outcome.risk == "transfer"
    async with factory() as session:
        feishu_pending = (
            (
                await session.execute(
                    select(PendingCommand).where(PendingCommand.transport == "feishu")
                )
            )
            .scalars()
            .first()
        )
        web_pending = (
            (await session.execute(select(PendingCommand).where(PendingCommand.transport == "web")))
            .scalars()
            .first()
        )
    assert feishu_pending is not None and web_pending is not None
    assert feishu_pending.payload_json == web_pending.payload_json
    assert feishu_pending.status == web_pending.status == PendingStatus.PENDING.value
    assert web_outcome.confirmation_code == web_pending.confirmation_code


async def test_logging_never_records_user_financial_text(
    factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    """P39 §56 — AI logs record request ids / intents / latency, never the raw
    user sentence (e.g. 工资18000) or provider keys."""
    ctx = await _identity(factory, "ou_log")
    command = _command(
        Action.CREATE,
        amount="18000.00",
        direction=Direction.INCOME,
        category="工资",
    )
    with caplog.at_level("INFO", logger="lark_ledger.services.ai_entry"):
        outcome = await _submit(
            factory, StubInterpreter(command), ctx, "工资18000", request_id="ai:log-1"
        )
    assert outcome.status is AIEntryStatus.EXECUTED
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "ai:log-1" in combined or combined == ""
    assert "工资18000" not in combined
    assert "sk-" not in combined
    assert "test-key" not in combined
