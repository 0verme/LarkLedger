"""P33 Web API coverage: /goals CRUD + /insights with auth & privacy.

HTTP-level tests against the real router:

* create / list / get / patch / delete / complete / archive / progress.
* unauthenticated → 401; cross-ledger goal id → 404 (IDOR guard).
* household privacy: B cannot see A's goal bound to A's private account
  (list filtered + get 404), and B's /insights never leak A's private data.
* /insights returns the deterministic structure; ?explain falls back when AI
  is not configured.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI
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
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.goals import GoalService
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
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
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
    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_owner", display_name="A")
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_member", display_name="B"
        )
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
        shared = (await AccountService(session).list(owner_ctx, include_archived=True))[0]
        private = await AccountService(session).create(
            owner_ctx,
            name="私房钱",
            account_type=AccountType.CASH,
            currency="CNY",
            opening_balance=Decimal("10000"),
            visibility=AccountVisibility.PRIVATE,
        )
        # Shared goal bound to the default shared account.
        shared_goal = await GoalService(session, timezone="Asia/Shanghai", currency="CNY").create(
            owner_ctx,
            name="家庭旅行基金",
            target_amount=Decimal("60000"),
            account_ids=[shared.id],
        )
        # Private goal bound to A's private account.
        private_goal = await GoalService(session, timezone="Asia/Shanghai", currency="CNY").create(
            owner_ctx,
            name="私密储备",
            target_amount=Decimal("20000"),
            account_ids=[private.id],
        )
        # Seeded spending history for an insight.
        now = datetime(2026, 8, 8, 4, tzinfo=UTC)
        for month_offset, amount in ((3, "1000"), (2, "1000"), (1, "1000")):
            session.add(
                LedgerEntry(
                    user_open_id="ou_owner",
                    created_by_user_id=owner.actor_user_id,
                    paid_by_user_id=owner.actor_user_id,
                    ledger_id=home.ledger.id,
                    account_id=private.id,
                    short_id=f"W{month_offset}",
                    amount=Decimal(amount),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="私人购物",
                    note="",
                    occurred_at=now - timedelta(days=30 * month_offset),
                    source_type="text",
                )
            )
        # This month's private spending jump (1500 vs baseline 1000).
        session.add(
            LedgerEntry(
                user_open_id="ou_owner",
                created_by_user_id=owner.actor_user_id,
                paid_by_user_id=owner.actor_user_id,
                ledger_id=home.ledger.id,
                account_id=private.id,
                short_id="W4",
                amount=Decimal("1500"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="私人购物",
                note="",
                occurred_at=now - timedelta(days=1),
                source_type="text",
            )
        )
        await session.commit()
        return {
            "owner_user_id": str(owner.actor_user_id),
            "member_user_id": str(member.actor_user_id),
            "household_id": str(home.household.id),
            "ledger_id": str(home.ledger.id),
            "shared_goal_id": str(shared_goal.id),
            "private_goal_id": str(private_goal.id),
        }


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


async def test_goal_crud_and_progress_web(factory: async_sessionmaker[AsyncSession]) -> None:
    ids = await _household(factory)
    client, csrf = await _client(factory, "ou_owner", ids["ledger_id"])
    headers = {"X-CSRF-Token": csrf}
    try:
        # list
        response = await client.get("/api/web/v1/goals")
        assert response.status_code == 200
        items = response.json()["items"]
        by_id = {item["id"]: item for item in items}
        assert ids["shared_goal_id"] in by_id
        assert ids["private_goal_id"] in by_id

        # create
        created = await client.post(
            "/api/web/v1/goals", headers=headers,
            json={
                "name": "MacBook",
                "target_amount": "15000.00",
                "target_date": "2027-06-30",
                "account_ids": [],
            },
        )
        assert created.status_code == 422  # savings goals require at least one account

        # get single goal with deterministic progress
        detail = await client.get(f"/api/web/v1/goals/{ids['shared_goal_id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["name"] == "家庭旅行基金"
        assert body["progress_percent"] == "0.00"
        assert body["is_target_reached"] is False

        # progress endpoint
        progress = await client.get(f"/api/web/v1/goals/{ids['shared_goal_id']}/progress")
        assert progress.status_code == 200
        assert progress.json()["target_amount"] == "60000.00"

        # patch
        updated = await client.patch(
            f"/api/web/v1/goals/{ids['shared_goal_id']}", headers=headers,
            json={"name": "家庭旅行基金 2027"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "家庭旅行基金 2027"

        # complete
        completed = await client.post(
            f"/api/web/v1/goals/{ids['shared_goal_id']}/complete", headers=headers
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"

        # archive
        archived = await client.post(
            f"/api/web/v1/goals/{ids['shared_goal_id']}/archive", headers=headers
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"

        # delete
        deleted = await client.delete(
            f"/api/web/v1/goals/{ids['shared_goal_id']}", headers=headers
        )
        assert deleted.status_code == 204
        gone = await client.get(f"/api/web/v1/goals/{ids['shared_goal_id']}")
        assert gone.status_code == 404
    finally:
        await client.aclose()


async def test_unauthenticated_goals_is_401(factory: async_sessionmaker[AsyncSession]) -> None:
    await _household(factory)
    auth = DashboardAuthService(settings(), factory)
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ledger.test")
    try:
        response = await client.get("/api/web/v1/goals")
        assert response.status_code == 401
        insights = await client.get("/api/web/v1/insights")
        assert insights.status_code == 401
    finally:
        await client.aclose()


async def test_privacy_private_goal_hidden_from_member_web(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _household(factory)
    member_client, member_csrf = await _client(factory, "ou_member", ids["ledger_id"])
    owner_client, owner_csrf = await _client(factory, "ou_owner", ids["ledger_id"])
    member_headers = {"X-CSRF-Token": member_csrf}
    try:
        # B's goal list contains only the shared goal.
        listed = await member_client.get("/api/web/v1/goals")
        assert listed.status_code == 200
        assert ids["private_goal_id"] not in {item["id"] for item in listed.json()["items"]}
        # B's direct get on the private goal → 404 (no IDOR, no inference).
        detail = await member_client.get(f"/api/web/v1/goals/{ids['private_goal_id']}")
        assert detail.status_code == 404
        progress = await member_client.get(f"/api/web/v1/goals/{ids['private_goal_id']}/progress")
        assert progress.status_code == 404
        # B cannot modify it either.
        patched = await member_client.patch(
            f"/api/web/v1/goals/{ids['private_goal_id']}", headers=member_headers,
            json={"name": "hacked"},
        )
        assert patched.status_code == 404
        # Owner still sees it.
        owner_detail = await owner_client.get(f"/api/web/v1/goals/{ids['private_goal_id']}")
        assert owner_detail.status_code == 200
        # 10000 opening − 4500 seeded private spending.
        assert owner_detail.json()["current_amount"] == "5500.00"
    finally:
        await member_client.aclose()
        await owner_client.aclose()


async def test_cross_ledger_goal_is_404_web(factory: async_sessionmaker[AsyncSession]) -> None:
    ids = await _household(factory)
    # A second, separate identity in a different ledger.
    client, _ = await _client(factory, "ou_outsider")
    try:
        detail = await client.get(f"/api/web/v1/goals/{ids['shared_goal_id']}")
        assert detail.status_code == 404
        progress = await client.get(f"/api/web/v1/goals/{ids['shared_goal_id']}/progress")
        assert progress.status_code == 404
    finally:
        await client.aclose()


async def test_insights_web_and_ai_fallback(factory: async_sessionmaker[AsyncSession]) -> None:
    ids = await _household(factory)
    owner_client, _ = await _client(factory, "ou_owner", ids["ledger_id"])
    member_client, _ = await _client(factory, "ou_member", ids["ledger_id"])
    try:
        # Owner sees their private spending change insight.
        owner_insights = await owner_client.get("/api/web/v1/insights?period=2026-08")
        assert owner_insights.status_code == 200
        owner_items = owner_insights.json()["insights"]
        assert owner_items  # spending-change from seeded private data
        types = {item["type"] for item in owner_items}
        assert "spending_change" in types
        # AI explanation is unconfigured → deterministic summary only, still 200.
        explained = await owner_client.get("/api/web/v1/insights?period=2026-08&explain=true")
        assert explained.status_code == 200
        for item in explained.json()["insights"]:
            assert item["summary"]
            assert item["explanation"] is None

        # Member's insights never leak A's private category.
        member_insights = await member_client.get("/api/web/v1/insights?period=2026-08")
        assert member_insights.status_code == 200
        for item in member_insights.json()["insights"]:
            assert item.get("related_category") != "私人购物"

        # Invalid period → 422; limit bounds enforced.
        bad = await owner_client.get("/api/web/v1/insights?period=2026-13")
        assert bad.status_code == 422
        limited = await owner_client.get("/api/web/v1/insights?limit=0")
        assert limited.status_code == 422
        capped = await owner_client.get("/api/web/v1/insights?limit=50")
        assert capped.status_code == 422
    finally:
        await owner_client.aclose()
        await member_client.aclose()


async def test_goal_conflict_returns_409(factory: async_sessionmaker[AsyncSession]) -> None:
    """Completing an already-completed goal is a 409, and invalid goal bodies
    are 422 — never silent no-ops."""
    ids = await _household(factory)
    client, csrf = await _client(factory, "ou_owner", ids["ledger_id"])
    headers = {"X-CSRF-Token": csrf}
    try:
        first = await client.post(
            f"/api/web/v1/goals/{ids['shared_goal_id']}/complete", headers=headers
        )
        assert first.status_code == 200
        second = await client.post(
            f"/api/web/v1/goals/{ids['shared_goal_id']}/complete", headers=headers
        )
        assert second.status_code == 409
        # Bad name → 422 (schema-level) and zero target → 422.
        bad = await client.post(
            "/api/web/v1/goals", headers=headers,
            json={"name": "", "target_amount": "1000", "account_ids": []},
        )
        assert bad.status_code == 422
        zero = await client.post(
            "/api/web/v1/goals", headers=headers,
            json={"name": "零目标", "target_amount": "0", "account_ids": []},
        )
        assert zero.status_code == 422
    finally:
        await client.aclose()
