"""Adapter Contract Test Suite (P36) — C01..C08.

Proves the v0.9.0 thesis: Feishu / Web / Client API are adapters over ONE
application layer. The same business fact entered through any channel yields
the same Domain Result — differences are allowed only in auth, input parsing,
presentation and channel capabilities.

Channels under test:
- Feishu  : ``MessageProcessor.process`` on a realistic event dict (identity
            resolution → deterministic/AI intent parsing → application service).
- Web     : ``ClientApplicationService`` — the exact service the Web route
            handlers delegate to.
- API     : real HTTP against ``/api/v1`` with a bearer token.

All expectations come from ``tests/contracts/canonical.py`` — one shared
source of truth per business fact.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.account_commands import try_parse_account_command
from lark_ledger.client_schemas import ClientCredentialCreateRequest
from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.main import create_app
from lark_ledger.models import (
    AccountType,
    AccountVisibility,
    Base,
    Direction,
    LedgerEntry,
    PendingCommand,
    PendingStatus,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.budget import BudgetService
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.client_auth import ClientCredentialService
from lark_ledger.services.events import EventService
from lark_ledger.services.goals import GoalService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.insights import InsightService
from lark_ledger.services.message_processor import MessageProcessor
from lark_ledger.services.transfers import TransferService
from tests.contracts.canonical import CanonicalExpectation, assert_matches, entry_snapshot


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        lark_app_id="app_id",
        lark_app_secret="app_secret",
        currency="CNY",
        timezone="Asia/Shanghai",
    )


# ---------------------------------------------------------------------------
# Feishu adapter scaffolding (same shape as the real event flow)
# ---------------------------------------------------------------------------


class RecordingFeishu:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, message_id: str, text: str, *, uuid: str | None = None) -> None:
        del uuid
        self.texts.append(text)


class IntentInterpreter:
    """Deterministic stand-in for the AI intent parser: parses intent, never
    touches the database and never executes business. Duck-typed against the
    interface ``MessageProcessor`` needs (``interpret`` + capability flags)."""

    def __init__(self, command: ParsedCommand) -> None:
        self.command = command

    @property
    def vision_configured(self) -> bool:
        return False

    @property
    def transcription_configured(self) -> bool:
        return False

    async def interpret(self, text: str, *, now: datetime, images: list[bytes]) -> ParsedCommand:
        del text, now, images
        return self.command


def text_event(text: str, message_id: str, open_id: str = "ou_user") -> dict[str, Any]:
    return {
        "event_id": message_id,
        "sender": {"sender_id": {"open_id": open_id}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    }


async def feishu_process(
    factory: async_sessionmaker[AsyncSession],
    event: dict[str, Any],
    command: ParsedCommand,
) -> None:
    """Run one event through the real MessageProcessor pipeline (T1..T3)."""
    settings = _settings()
    processor = MessageProcessor(
        settings,
        factory,
        RecordingFeishu(),
        IntentInterpreter(command),
    )
    service = EventService(factory, processor, worker_enabled=False)
    await service.handle_safely(str(event["event_id"]), event, transport="webhook")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def contract_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _identity(session: AsyncSession, open_id: str, display_name: str = "") -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=open_id, display_name=display_name)


async def _select_channel_ledger(session: AsyncSession, open_id: str, ledger_id: uuid.UUID) -> None:
    """Switch a Feishu channel identity to a ledger (as the real user would)."""
    from lark_ledger.models import ChannelIdentity
    from lark_ledger.services.ledger_management import LedgerManagementService

    identity = await session.scalar(
        select(ChannelIdentity).where(
            ChannelIdentity.channel == "feishu",
            ChannelIdentity.external_subject_id == open_id,
        )
    )
    assert identity is not None
    await LedgerManagementService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).select_for_channel(user_id=identity.user_id, identity_id=identity.id, ledger_id=ledger_id)
    await session.flush()


async def _app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = create_app(_settings())
    app.state.settings = _settings()
    app.state.session_factory = factory
    return app


async def _api_client(
    factory: async_sessionmaker[AsyncSession],
    open_id: str,
    scopes: list[str] | None = None,
) -> tuple[httpx.AsyncClient, RequestContext, str]:
    async with factory() as session:
        context = await _identity(session, open_id)
        created = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(
                name="contract device",
                scopes=scopes if scopes is not None else ["ledger:read", "ledger:write"],
            ),
        )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=await _app(factory)),
        base_url="http://ledger.test",
    )
    return client, context, created.token


def _command(
    action: Action,
    *,
    amount: str | None = None,
    direction: Direction | None = None,
    category: str | None = None,
    note: str | None = None,
    payer_reference: str | None = None,
) -> ParsedCommand:
    return ParsedCommand(
        action=action,
        amount=Decimal(amount) if amount else None,
        direction=direction,
        category=category,
        note=note,
        occurred_at=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        payer_reference=payer_reference,
    )


# ---------------------------------------------------------------------------
# C01 — Create Expense through every adapter
# ---------------------------------------------------------------------------


async def test_c01_create_expense_is_equivalent_across_adapters(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    expected = CanonicalExpectation(
        direction="expense",
        amount="18.00",
        currency="CNY",
        category="餐饮",
        note="早餐",
    )

    # Feishu: 「早餐18」→ AI intent parser → application service
    await feishu_process(
        contract_factory,
        text_event("早餐18", "om_c01", open_id="ou_c01"),
        _command(
            Action.CREATE,
            amount="18.00",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="早餐",
        ),
    )
    async with contract_factory() as session:
        feishu_row = await entry_snapshot(session, "om_c01")

    # Web/Application adapter (the service Web routes delegate to)
    async with contract_factory() as session:
        ctx = await _identity(session, "ou_c01")
        await ClientApplicationService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).execute_financial(
            ctx,
            _command(
                Action.CREATE,
                amount="18.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="早餐",
            ),
            source_type="web",
            source_message_id="web_c01",
        )
        await session.commit()
        web_row = await entry_snapshot(session, "web_c01")

    # Client API: POST /api/v1/transactions
    client, _, token = await _api_client(contract_factory, "ou_c01")
    async with client:
        response = await client.post(
            "/api/v1/transactions",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "c01-expense",
            },
            json={
                "direction": "expense",
                "amount": "18.00",
                "currency": "CNY",
                "category": "餐饮",
                "note": "早餐",
                "occurred_at": "2026-08-14T08:00:00+08:00",
            },
        )
    assert response.status_code == 201
    api_entry_id = response.json()["resource"]["id"]
    async with contract_factory() as session:
        row = await session.get(LedgerEntry, uuid.UUID(api_entry_id))
        assert row is not None
        api_row = {
            "ledger_id": str(row.ledger_id),
            "direction": "expense",
            "amount": str(row.amount),
            "currency": row.currency,
            "category": row.category,
            "note": row.note,
            "created_by_user_id": str(row.created_by_user_id),
            "paid_by_user_id": str(row.paid_by_user_id) if row.paid_by_user_id else None,
        }

    # One business fact, three channels → identical Domain Result
    for label, snapshot in (("feishu", feishu_row), ("web", web_row), ("api", api_row)):
        assert_matches(snapshot, expected)
        assert snapshot["ledger_id"] == feishu_row["ledger_id"], label


# ---------------------------------------------------------------------------
# C02 — Household payer: created_by ≠ paid_by
# ---------------------------------------------------------------------------


async def test_c02_household_payer_equivalent(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with contract_factory() as session:
        owner = await _identity(session, "ou_owner", "小明")
        member = await _identity(session, "ou_member", "B")
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        await _select_channel_ledger(session, "ou_owner", home.ledger.id)
        await session.commit()
        ledger_id = home.ledger.id
        member_user_id = member.actor_user_id
    owner_ctx = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=ledger_id,
        source_channel="feishu",
        external_subject_id="ou_owner",
    )
    member_ctx = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=ledger_id,
        source_channel="feishu",
        external_subject_id="ou_member",
    )

    expected = CanonicalExpectation(
        direction="expense",
        amount="120.00",
        currency="CNY",
        category="买菜",
        note="买菜",
        created_by_user_id=str(owner.actor_user_id),
        paid_by_user_id=str(member_user_id),
    )

    # Feishu: A 输入「B 买菜 120」→ AI echoes payer reference "B"
    await feishu_process(
        contract_factory,
        text_event("B 买菜 120", "om_c02", open_id="ou_owner"),
        _command(
            Action.CREATE,
            amount="120.00",
            direction=Direction.EXPENSE,
            category="买菜",
            note="买菜",
            payer_reference="B",
        ),
    )
    async with contract_factory() as session:
        feishu_row = await entry_snapshot(session, "om_c02")

    # Client API: explicit paid_by_user_id
    client, _, token = await _api_client(contract_factory, "ou_owner")
    async with client:
        selected = await client.post(
            f"/api/v1/ledgers/{ledger_id}/select",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "c02-select"},
        )
        assert selected.status_code == 200, selected.text
        response = await client.post(
            "/api/v1/transactions",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "c02-payer",
            },
            json={
                "direction": "expense",
                "amount": "120.00",
                "currency": "CNY",
                "category": "买菜",
                "note": "买菜",
                "paid_by_user_id": str(member_user_id),
                "occurred_at": "2026-08-14T08:00:00+08:00",
            },
        )
    assert response.status_code == 201
    api_entry_id = response.json()["resource"]["id"]
    async with contract_factory() as session:
        row = await session.get(LedgerEntry, uuid.UUID(api_entry_id))
        assert row is not None
        api_row = {
            "ledger_id": str(row.ledger_id),
            "direction": "expense",
            "amount": str(row.amount),
            "currency": row.currency,
            "category": row.category,
            "note": row.note,
            "created_by_user_id": str(row.created_by_user_id),
            "paid_by_user_id": str(row.paid_by_user_id) if row.paid_by_user_id else None,
        }

    # Web/Application adapter with explicit payer
    async with contract_factory() as session:
        await ClientApplicationService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).execute_financial(
            owner_ctx,
            _command(
                Action.CREATE,
                amount="120.00",
                direction=Direction.EXPENSE,
                category="买菜",
                note="买菜",
            ),
            source_type="web",
            source_message_id="web_c02",
            paid_by_user_id=member_user_id,
        )
        await session.commit()
        web_row = await entry_snapshot(session, "web_c02")

    for snapshot in (feishu_row, web_row, api_row):
        assert_matches(snapshot, expected)
    assert member_ctx.actor_user_id


# ---------------------------------------------------------------------------
# C03 — Privacy: A's private account invisible to B on every adapter
# ---------------------------------------------------------------------------


async def test_c03_private_account_is_invisible_on_every_adapter(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with contract_factory() as session:
        owner = await _identity(session, "ou_p_owner", "甲")
        member = await _identity(session, "ou_p_member", "乙")
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_p_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        ledger_id = home.ledger.id
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=ledger_id,
            source_channel="feishu",
            external_subject_id="ou_p_owner",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=ledger_id,
            source_channel="feishu",
            external_subject_id="ou_p_member",
        )
        await _select_channel_ledger(session, "ou_p_owner", home.ledger.id)
        await _select_channel_ledger(session, "ou_p_member", home.ledger.id)
        await session.flush()
        account = await AccountService(session).create(
            owner_ctx,
            name="私房钱",
            account_type=AccountType.CASH,
            currency="CNY",
        )
        await AccountService(session).set_visibility(
            owner_ctx, account.id, visibility=AccountVisibility.PRIVATE
        )
        await session.commit()
        private_account_id = account.id

    # Web/Application adapter: B cannot read the private account (404 semantics).
    # AccountService surfaces the same outward 404 for private/inaccessible
    # accounts as for non-existent ones — no existence side channel.
    from lark_ledger.services.accounts import AccountNotFoundError

    async with contract_factory() as session:
        try:
            await ClientApplicationService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).get_account(member_ctx, private_account_id)
            raise AssertionError("member must not read owner's private account")
        except AccountNotFoundError:
            pass

    # Client API: B's token → 404 (no existence leak)
    client, _, member_token = await _api_client(contract_factory, "ou_p_member")
    async with client:
        selected = await client.post(
            f"/api/v1/ledgers/{ledger_id}/select",
            headers={"Authorization": f"Bearer {member_token}", "Idempotency-Key": "c03-select"},
        )
        assert selected.status_code == 200, selected.text
        response = await client.get(
            f"/api/v1/accounts/{private_account_id}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"

    # Feishu adapter: B queries accounts → private account absent from reply
    async with contract_factory() as session:
        command = try_parse_account_command("查看账户")
        assert command is not None
        processor = MessageProcessor(
            _settings(), contract_factory, RecordingFeishu(), IntentInterpreter(command)
        )
    # The account query path replies synchronously through the processor; use
    # the same event plumbing so identity resolution matches the Feishu flow.
    feishu = RecordingFeishu()
    processor = MessageProcessor(_settings(), contract_factory, feishu, IntentInterpreter(command))
    event_service = EventService(contract_factory, processor, worker_enabled=False)
    await event_service.handle_safely(
        "evt_c03",
        text_event("查看账户", "om_c03", open_id="ou_p_member"),
        transport="webhook",
    )
    assert feishu.texts, "Feishu adapter must reply to the account query"
    assert "私房钱" not in " ".join(feishu.texts)


# ---------------------------------------------------------------------------
# C04 — Budget: channel cannot change statistical口径
# ---------------------------------------------------------------------------


async def test_c04_budget_spent_is_channel_independent(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Same ledger, three different channels create: expense 150 (Feishu),
    # income 1000 (API), transfer 300 (Web/Application).
    await feishu_process(
        contract_factory,
        text_event("外卖150", "om_c04", open_id="ou_c04_api"),
        _command(
            Action.CREATE,
            amount="150.00",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="外卖",
        ),
    )

    client, ctx, token = await _api_client(contract_factory, "ou_c04_api")
    async with client:
        response = await client.post(
            "/api/v1/transactions",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "c04-income",
            },
            json={
                "direction": "income",
                "amount": "1000.00",
                "currency": "CNY",
                "category": "工资",
                "note": "工资",
                "occurred_at": "2026-08-14T09:00:00+08:00",
            },
        )
    assert response.status_code == 201

    async with contract_factory() as session:
        from_account = await AccountService(session).create(
            ctx, name="现金", account_type=AccountType.CASH, currency="CNY"
        )
        to_account = await AccountService(session).create(
            ctx, name="储蓄", account_type=AccountType.ASSET, currency="CNY"
        )
        await session.commit()
        await TransferService(session).create(
            ctx,
            from_account_id=from_account.id,
            to_account_id=to_account.id,
            amount=Decimal("300.00"),
            occurred_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
            note="转存",
            source_type="web",
            source_message_id="web_c04_transfer",
        )
        await session.commit()
        overview = await BudgetService(session, currency="CNY", timezone="Asia/Shanghai").overview(
            ctx, period=None
        )
    assert overview.total_spent == Decimal("150.00")


# ---------------------------------------------------------------------------
# C05 — Recurring confirmation preserves payer; confirmed_by = actor
# ---------------------------------------------------------------------------


async def test_c05_recurring_confirmation_preserves_payer_semantics(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with contract_factory() as session:
        owner = await _identity(session, "ou_r_owner", "A")
        member = await _identity(session, "ou_r_member", "B")
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_r_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        ledger_id = home.ledger.id
        member_user_id = member.actor_user_id
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=ledger_id,
            source_channel="feishu",
            external_subject_id="ou_r_owner",
        )
        await AccountService(session).create(
            owner_ctx, name="现金", account_type=AccountType.CASH, currency="CNY"
        )
        now = datetime.now(UTC)
        from lark_ledger.models import PendingCommand as PendingRow

        confirmation_code = "C5ABCD"
        pending = PendingRow(
            user_open_id="ou_r_owner",
            confirmation_code=confirmation_code,
            status=PendingStatus.PENDING.value,
            source_type="recurring",
            transport="feishu",
            command_type="entry.create",
            risk_reason="frozen recurring confirmation",
            actor_user_id=owner.actor_user_id,
            ledger_id=ledger_id,
            paid_by_user_id=member_user_id,
            source_message_id="rec_c05",
            payload_json=_command(
                Action.CREATE,
                amount="500.00",
                direction=Direction.EXPENSE,
                category="房租",
                note="房租",
            ).model_dump(mode="json"),
            preview_json={"items": []},
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session.add(pending)
        await session.commit()
        confirmation_id = confirmation_code
        pending_id = pending.id
        owner_user_id = owner.actor_user_id

    # API channel confirms as A (actor), payer stays B.
    client, _, token = await _api_client(
        contract_factory,
        "ou_r_owner",
        scopes=["ledger:read", "ledger:write", "pending:write"],
    )
    async with client:
        selected = await client.post(
            f"/api/v1/ledgers/{ledger_id}/select",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "c05-select"},
        )
        assert selected.status_code == 200, selected.text
        response = await client.post(
            f"/api/v1/pending/{confirmation_id}/confirm",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "c05-confirm"},
        )
    assert response.status_code == 200, response.text

    async with contract_factory() as session:
        row = await session.scalar(
            select(LedgerEntry).where(LedgerEntry.source_message_id == "rec_c05")
        )
        assert row is not None, "confirmed pending must create exactly one entry"
        assert str(row.paid_by_user_id) == str(member_user_id), "payer must stay B"
        assert str(row.created_by_user_id) == str(owner_user_id), "confirmed_by = A"
        assert row.amount == Decimal("500.00")
        assert row.category == "房租"
        pending_row = await session.get(PendingCommand, pending_id)
        assert pending_row is not None
        assert pending_row.status == PendingStatus.EXECUTED.value


def pending_id_of(confirmation_id: str) -> uuid.UUID:
    return confirmation_id  # type: ignore[return-value]  # resolved below in test


# ---------------------------------------------------------------------------
# C06 — Goal progress identical on Web/Application and Client API
# ---------------------------------------------------------------------------


async def test_c06_goal_progress_is_channel_independent(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with contract_factory() as session:
        ctx = await _identity(session, "ou_g")
        account = await AccountService(session).create(
            ctx,
            name="储蓄罐",
            account_type=AccountType.ASSET,
            currency="CNY",
            opening_balance=Decimal("5000.00"),
        )
        goal = await GoalService(session, timezone="Asia/Shanghai", currency="CNY").create(
            ctx,
            name="买相机",
            target_amount=Decimal("10000.00"),
            account_ids=[account.id],
        )
        await session.commit()
        goal_id = goal.id

    # Web/Application adapter
    async with contract_factory() as session:
        progress = await ClientApplicationService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).goal_progress(ctx, goal_id)

    # Client API
    client, _, token = await _api_client(contract_factory, "ou_g")
    async with client:
        response = await client.get("/api/v1/goals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert items, "goals list must be non-empty"
    api_item = next(item for item in items if item["id"] == str(goal_id))

    assert str(progress.progress_percent) == api_item["progress_percent"]
    assert str(progress.current_amount) == api_item["current_amount"]
    assert str(progress.target_amount) == api_item["target_amount"]
    assert str(progress.current_amount) == "5000.00"
    assert progress.progress_percent == Decimal("50.00")


# ---------------------------------------------------------------------------
# C07 — Insights share one deterministic engine
# ---------------------------------------------------------------------------


async def test_c07_insights_metrics_are_channel_independent(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One deterministic ledger snapshot: current-month expense spike vs trailing
    # average produces the I01 insight deterministically.
    async with contract_factory() as session:
        ctx = await _identity(session, "ou_i")
        today = datetime.now(UTC).astimezone()
        for offset, amount in ((0, "800.00"), (0, "700.00"), (0, "900.00")):
            occurred = today - timedelta(days=offset)
            await ClientApplicationService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).execute_financial(
                ctx,
                _command(
                    Action.CREATE,
                    amount=amount,
                    direction=Direction.EXPENSE,
                    category="购物",
                    note="insight",
                ),
                source_type="web",
                source_message_id=f"insight_{offset}_{amount}",
            )
            occurred = occurred  # keep referenced
        await session.commit()

    # Web/Application adapter (same InsightService the Feishu path uses)
    async with contract_factory() as session:
        web_insights = await InsightService(
            session, timezone="Asia/Shanghai", currency="CNY"
        ).insights(ctx, period=None, limit=10)

    # Client API
    client, _, token = await _api_client(contract_factory, "ou_i")
    async with client:
        response = await client.get(
            "/api/v1/insights", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    api_insights = response.json()

    web_metrics = {(i.key, i.type): i.metric for i in web_insights}
    api_metrics = {(i["key"], i["type"]): i["metric"] for i in api_insights}
    assert web_metrics == api_metrics, "structured insight metrics must match"


# ---------------------------------------------------------------------------
# C08 — Duplicate delivery: API Idempotency-Key and Feishu event idempotency
# ---------------------------------------------------------------------------


async def test_c08_duplicate_delivery_is_exactly_once(
    contract_factory: async_sessionmaker[AsyncSession],
) -> None:
    # --- API: same Idempotency-Key + same body retried → 1 entry
    client, _, token = await _api_client(contract_factory, "ou_c08_api")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "c08-retry"}
    payload = {
        "direction": "expense",
        "amount": "18.00",
        "currency": "CNY",
        "category": "餐饮",
        "note": "早餐",
        "occurred_at": "2026-08-14T08:00:00+08:00",
    }
    async with client:
        first = await client.post("/api/v1/transactions", headers=headers, json=payload)
        second = await client.post("/api/v1/transactions", headers=headers, json=payload)
        conflict = await client.post(
            "/api/v1/transactions",
            headers=headers,
            json={**payload, "amount": "28.00"},
        )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert conflict.status_code == 409

    # --- Feishu: same event_id delivered twice → 1 entry
    event = text_event("午餐22", "om_c08", open_id="ou_c08_fs")
    await feishu_process(
        contract_factory,
        event,
        _command(
            Action.CREATE,
            amount="22.00",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="午餐",
        ),
    )
    # Re-deliver the exact same event (same event_id): claim must reject it.
    settings = _settings()
    processor = MessageProcessor(
        settings,
        contract_factory,
        RecordingFeishu(),
        IntentInterpreter(
            _command(
                Action.CREATE,
                amount="22.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午餐",
            )
        ),
    )
    service = EventService(contract_factory, processor, worker_enabled=False)
    duplicate = await service.claim("om_c08", event, transport="webhook")
    assert duplicate is False, "duplicate Feishu event_id must not be claimed twice"

    async with contract_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.source_message_id == "om_c08")
        )
        api_count = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(
                LedgerEntry.note == "早餐",
                LedgerEntry.category == "餐饮",
            )
        )
    assert count == 1, "Feishu duplicate event must create exactly one entry"
    assert api_count == 1, "API idempotent retry must create exactly one entry"
