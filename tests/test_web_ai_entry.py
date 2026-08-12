"""P39 — Web AI Entry API tests (WAIP1–WAIP12).

The browser-facing natural-language endpoint contract:

    POST /api/web/v1/ai/entries  (CSRF + Idempotency-Key + server-side session)

Everything runs through the Unified AI Entry (never a raw repository), the
actor/ledger come from the session, and write retries replay the stored
canonical response exactly-once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import AccountType, Base, Direction, LedgerEntry, PendingCommand
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ai import CommandInterpretationError
from lark_ledger.services.ai_entry import UnifiedAIEntryService
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.web_api import _auth_service, router


def settings() -> Settings:
    return Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="test-only-secret-that-is-long-enough-123456",
        dashboard_cookie_secure=False,
        dashboard_admin_open_ids="",
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
        pending_expires_seconds=3600,
        currency="CNY",
        timezone="Asia/Shanghai",
        pending_enabled=True,
    )


class StubInterpreter:
    def __init__(self, command: ParsedCommand | None = None, error: Exception | None = None):
        self.command = command
        self.error = error

    @property
    def vision_configured(self) -> bool:
        return False

    @property
    def transcription_configured(self) -> bool:
        return False

    async def interpret(
        self, text: str, *, now: datetime, images: list[bytes] | None = None
    ) -> ParsedCommand:
        if self.error is not None:
            raise self.error
        if self.command is None:
            raise AssertionError("stub needs a command")
        return self.command


def _command(
    action: Action,
    *,
    amount: str | None = None,
    direction: Direction | None = None,
    category: str | None = None,
    note: str | None = None,
    account_hint: str | None = None,
    from_account_hint: str | None = None,
    to_account_hint: str | None = None,
    limit: int | None = None,
) -> ParsedCommand:
    return ParsedCommand(
        action=action,
        amount=Decimal(amount) if amount is not None else None,
        direction=direction,
        category=category,
        note=note,
        occurred_at=(
            datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
            if action in {Action.CREATE, Action.TRANSFER}
            else None
        ),
        account_hint=account_hint,
        from_account_hint=from_account_hint,
        to_account_hint=to_account_hint,
        limit=limit,
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


async def _client(
    factory: async_sessionmaker[AsyncSession],
    user: str,
    *,
    interpreter: StubInterpreter,
) -> tuple[httpx.AsyncClient, str]:
    auth = DashboardAuthService(settings(), factory)
    created = await auth.create_session({"open_id": user, "name": user, "avatar_url": ""})
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.state.ai_entry_service = UnifiedAIEntryService(settings(), factory, interpreter=interpreter)
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created.csrf_token


def _csrf(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token}


async def _entry_count(
    factory: async_sessionmaker[AsyncSession], ledger_id: uuid.UUID | None = None
) -> int:
    async with factory() as session:
        query = select(func.count()).select_from(LedgerEntry)
        if ledger_id is not None:
            query = query.where(LedgerEntry.ledger_id == ledger_id)
        return int(await session.scalar(query) or 0)


async def test_waip01_executed_create_expense(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip01",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip01-key"},
            json={"text": "午饭28"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["operation"] == "create"
    assert body["amount"] == "28.00"
    assert body["direction"] == "expense"
    assert body["request_id"].startswith("ai:")
    assert body["replayed"] is False
    assert await _entry_count(factory) == 1


async def test_waip02_confirmation_required_for_transfer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip02",
        interpreter=StubInterpreter(
            _command(
                Action.TRANSFER,
                amount="1000.00",
                from_account_hint="招行",
                to_account_hint="支付宝",
            )
        ),
    )
    async with client:
        # Resolve the transfer accounts first.
        async with factory() as session:
            context = await IdentityService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_waip02")
            from lark_ledger.services.accounts import AccountService

            await AccountService(session).create(
                context, name="招行", account_type=AccountType.ASSET, currency="CNY"
            )
            await AccountService(session).create(
                context, name="支付宝", account_type=AccountType.ASSET, currency="CNY"
            )
            await session.commit()
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip02-key"},
            json={"text": "从招行转1000到支付宝"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmation_required"
    assert body["pending_command_id"] is not None
    assert body["risk"] == "transfer"
    async with factory() as session:
        pending = await session.scalar(select(PendingCommand))
        assert pending is not None
        assert pending.transport == "web"
        assert pending.status == "pending"
    assert await _entry_count(factory) == 0


async def test_waip03_clarification_required(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip03",
        interpreter=StubInterpreter(_command(Action.HELP)),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip03-key"},
            json={"text": "记一笔28"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification_required"
    assert "我可以帮你记账" in body["message"]


async def test_waip04_error_envelope_is_safe_chinese(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # The AI names an account that does not exist: resolution fails during
    # execution and the safe Chinese envelope is returned (never a 500 / never
    # a raw exception string).
    client, csrf = await _client(
        factory,
        "ou_waip04",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                account_hint="不存在的账户",
            )
        ),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip04-key"},
            json={"text": "午饭28"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "账户名称不明确" in body["message"]
    assert body["request_id"]


async def test_waip05_parse_failure_is_clarification_not_500(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip05",
        interpreter=StubInterpreter(error=CommandInterpretationError("bad")),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip05-key"},
            json={"text": "乱写一段话"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "clarification_required"
    assert await _entry_count(factory) == 0


async def test_waip06_idempotent_replay_is_exactly_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip06",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="午饭",
            )
        ),
    )
    headers = {**_csrf(csrf), "Idempotency-Key": "waip06-key"}
    async with client:
        first = await client.post(
            "/api/web/v1/ai/entries", headers=headers, json={"text": "午饭28"}
        )
        second = await client.post(
            "/api/web/v1/ai/entries", headers=headers, json={"text": "午饭28"}
        )
        conflict = await client.post(
            "/api/web/v1/ai/entries",
            headers=headers,
            json={"text": "工资18000"},
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["status"] == "executed"
    assert first.json()["request_id"] == second.json()["request_id"]
    assert conflict.status_code == 409
    assert await _entry_count(factory) == 1


async def test_waip07_requires_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf_token = await _client(
        factory,
        "ou_waip07",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
    )
    client.cookies.delete(SESSION_COOKIE)
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={"X-CSRF-Token": "x"},
            json={"text": "午饭28"},
        )
    # csrf_principal maps any auth failure (missing session / bad CSRF) to 403,
    # matching the P38 write-path contract.
    assert response.status_code == 403


async def test_waip08_requires_csrf(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf_token = await _client(
        factory,
        "ou_waip08",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={"Idempotency-Key": "waip08-key"},
            json={"text": "午饭28"},
        )
    assert response.status_code == 403


async def test_waip09_requires_idempotency_key(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip09",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers=_csrf(csrf),
            json={"text": "午饭28"},
        )
    assert response.status_code == 422


async def test_waip10_writes_to_the_session_current_ledger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip10",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="86.00",
                direction=Direction.EXPENSE,
                category="买菜",
                note="买菜",
            )
        ),
    )
    async with client:
        listed = await client.get("/api/web/v1/ledgers")
        personal = [item for item in listed.json()["items"] if item["kind"] == "personal"][0]
        await client.post(
            f"/api/web/v1/ledgers/{personal['id']}/select",
            headers=_csrf(csrf),
        )
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip10-key"},
            json={"text": "买菜86"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert await _entry_count(factory, uuid.UUID(personal["id"])) == 1


async def test_waip11_ledger_switch_routes_ai_writes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """After switching to the household ledger, AI writes land there and only
    there (P39 §17/§47)."""
    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_waip11", display_name="甲")
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_waip11_b", display_name="乙"
        )
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_waip11_b")
        await manager.accept(member.actor_user_id, invitation.public_id)
        await session.commit()
        household_ledger_id = home.ledger.id
        personal_ledger_id = owner.ledger_id

    client, csrf = await _client(
        factory,
        "ou_waip11",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="86.00",
                direction=Direction.EXPENSE,
                category="买菜",
                note="买菜",
            )
        ),
    )
    async with client:
        selected = await client.post(
            f"/api/web/v1/ledgers/{household_ledger_id}/select",
            headers=_csrf(csrf),
        )
        assert selected.status_code == 200
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip11-key"},
            json={"text": "买菜86"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert await _entry_count(factory, household_ledger_id) == 1
    assert await _entry_count(factory, personal_ledger_id) == 0


async def test_waip12_rejects_oversized_input(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(
        factory,
        "ou_waip12",
        interpreter=StubInterpreter(
            _command(
                Action.CREATE,
                amount="28.00",
                direction=Direction.EXPENSE,
                category="餐饮",
            )
        ),
    )
    async with client:
        response = await client.post(
            "/api/web/v1/ai/entries",
            headers={**_csrf(csrf), "Idempotency-Key": "waip12-key"},
            json={"text": "x" * 501},
        )
    assert response.status_code == 422
    assert await _entry_count(factory) == 0
