"""Web API coverage for P30: member alias management + member stats."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.web_api import _auth_service, router


def settings() -> Settings:
    return Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="test-only-secret-that-is-long-enough-123456",
        dashboard_cookie_secure=False,
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
        pending_expires_seconds=3600,
        currency="CNY",
        timezone="Asia/Shanghai",
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


async def _household(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """Bootstrap owner + member + household; return ids keyed by role."""
    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_owner", display_name="A")
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_member", display_name="B"
        )
        from lark_ledger.context import RequestContext
        from lark_ledger.services.household_management import HouseholdManagementService

        manager = HouseholdManagementService(
            session, currency="CNY", timezone="Asia/Shanghai"
        )
        home = await manager.create(owner.actor_user_id, "测试家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_owner",
        )
        await LedgerService(session, commit_changes=False).execute(
            owner_ctx,
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("120"),
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
                payer_reference="B",
            ),
        )
        await session.commit()
        result = {
            "owner_user_id": str(owner.actor_user_id),
            "member_user_id": str(member.actor_user_id),
            "household_id": str(home.household.id),
            "ledger_id": str(home.ledger.id),
        }
    return result


async def _client(
    factory: async_sessionmaker[AsyncSession], open_id: str, ledger_id: str | None = None
) -> tuple[httpx.AsyncClient, str]:
    auth = DashboardAuthService(settings(), factory)
    created = await auth.create_session({"open_id": open_id, "name": "用户", "avatar_url": ""})
    if ledger_id is not None:
        from lark_ledger.models import DashboardSession

        async with factory() as session:
            row = await session.get(DashboardSession, uuid.UUID(created.principal.session_id))
            assert row is not None
            row.ledger_id = uuid.UUID(ledger_id)
            await session.commit()
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created.csrf_token


async def test_member_alias_set_and_member_stats_web(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _household(factory)
    client, csrf = await _client(factory, "ou_owner")
    headers = {"X-CSRF-Token": csrf}
    try:
        updated = await client.patch(
            f"/api/web/v1/households/{ids['household_id']}/members/{ids['member_user_id']}",
            headers=headers,
            json={"alias": "老婆"},
        )
        assert updated.status_code == 200
        assert updated.json()["alias"] == "老婆"

        stats = await client.get(
            f"/api/web/v1/ledgers/{ids['ledger_id']}/members/stats"
        )
        assert stats.status_code == 200
        items = stats.json()
        by_user = {item["user_id"]: item for item in items}
        assert by_user[ids["member_user_id"]]["expense_total"] == "120.00"
        assert by_user[ids["member_user_id"]]["alias"] == "老婆"

        # Clearing the alias works and the member reverts to display name.
        cleared = await client.patch(
            f"/api/web/v1/households/{ids['household_id']}/members/{ids['member_user_id']}",
            headers=headers,
            json={"alias": ""},
        )
        assert cleared.status_code == 200
        assert cleared.json()["alias"] is None
    finally:
        await client.aclose()


async def test_member_stats_requires_membership(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _household(factory)
    # An outsider (neither owner nor member) gets 404 for the household ledger.
    client, _ = await _client(factory, "ou_outsider")
    try:
        response = await client.get(
            f"/api/web/v1/ledgers/{ids['ledger_id']}/members/stats"
        )
        assert response.status_code == 404
    finally:
        await client.aclose()


async def test_member_alias_requires_owner(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _household(factory)
    client, csrf = await _client(factory, "ou_member")
    headers = {"X-CSRF-Token": csrf}
    try:
        response = await client.patch(
            f"/api/web/v1/households/{ids['household_id']}/members/{ids['owner_user_id']}",
            headers=headers,
            json={"alias": "家长"},
        )
        assert response.status_code == 403
    finally:
        await client.aclose()


async def test_overview_web(factory: async_sessionmaker[AsyncSession]) -> None:
    ids = await _household(factory)
    client, _ = await _client(factory, "ou_owner", ledger_id=ids["ledger_id"])
    try:
        response = await client.get("/api/web/v1/overview?period=2026-08")
        assert response.status_code == 200
        body = response.json()
        assert body["period"] == "2026-08"
        assert body["expense_total"] == "120.00"
        assert body["ledger_kind"] == "household_shared"
        assert body["member_contributions"][0]["user_id"] == ids["member_user_id"]
        assert body["recent_transactions"][0]["payer_name"] == "B"
    finally:
        await client.aclose()


async def test_account_visibility_web(factory: async_sessionmaker[AsyncSession]) -> None:
    """P32: private accounts hide from other members via the web API; the
    visibility endpoint is owner-only."""
    ids = await _household(factory)
    owner_client, owner_csrf = await _client(factory, "ou_owner", ledger_id=ids["ledger_id"])
    member_client, member_csrf = await _client(factory, "ou_member", ledger_id=ids["ledger_id"])
    headers = {"X-CSRF-Token": owner_csrf}
    try:
        # Owner creates a private account.
        created = await owner_client.post(
            "/api/web/v1/accounts",
            headers=headers,
            json={"name": "私房钱", "type": "cash", "visibility": "private"},
        )
        assert created.status_code == 201
        account_id = created.json()["id"]
        assert created.json()["visibility"] == "private"
        assert created.json()["owner_user_id"] == ids["owner_user_id"]

        # Member's account list hides it.
        member_accounts = await member_client.get("/api/web/v1/accounts")
        assert member_accounts.status_code == 200
        assert account_id not in {
            item["id"] for item in member_accounts.json()["items"]
        }
        hidden = await member_client.get(f"/api/web/v1/accounts/{account_id}")
        assert hidden.status_code == 404

        # A member cannot toggle it; the owner can flip it back to shared.
        denied = await member_client.post(
            f"/api/web/v1/accounts/{account_id}/visibility",
            headers={"X-CSRF-Token": member_csrf},
            json={"visibility": "shared"},
        )
        assert denied.status_code == 404
        toggled = await owner_client.post(
            f"/api/web/v1/accounts/{account_id}/visibility",
            headers=headers,
            json={"visibility": "shared"},
        )
        assert toggled.status_code == 200
        assert toggled.json()["visibility"] == "shared"
        member_accounts = await member_client.get("/api/web/v1/accounts")
        assert account_id in {item["id"] for item in member_accounts.json()["items"]}
    finally:
        await owner_client.aclose()
        await member_client.aclose()
