"""P29 recurring-rule Web API tests (authenticated dashboard endpoints)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.identity import IdentityService
from lark_ledger.web_api import _auth_service, router

FUTURE = "2027-01-15"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="test-only-secret-that-is-long-enough-123456",
        dashboard_cookie_secure=False,
        dashboard_admin_open_ids="ou_admin",
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
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
) -> tuple[httpx.AsyncClient, str]:
    auth = DashboardAuthService(settings(), factory)
    created = await auth.create_session({"open_id": user, "name": user, "avatar_url": ""})
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


async def _default_account_id(factory: async_sessionmaker[AsyncSession], user: str) -> str:
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id=user)
        await session.commit()
        account = await AccountService(session).get_default(context)
        await session.commit()
        return str(account.id)


def _create_payload(
    account_id: str, *, category: str = "房租", amount: str = "3500"
) -> dict[str, object]:
    return {
        "transaction_type": "expense",
        "amount": amount,
        "currency": None,
        "category": category,
        "description": category,
        "frequency": "monthly",
        "interval": 1,
        "next_occurrence": FUTURE,
        "account_id": account_id,
    }


async def test_recurring_rules_crud_flow(factory: async_sessionmaker[AsyncSession]) -> None:
    account_id = await _default_account_id(factory, "ou_recur_web")
    client, csrf = await _client(factory, "ou_recur_web")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        created = await client.post(
            "/api/web/v1/recurring-rules", headers=headers, json=_create_payload(account_id)
        )
        assert created.status_code == 201
        body = created.json()
        assert body["category"] == "房租"
        assert body["amount"] == "3500.00"
        assert body["frequency"] == "monthly"
        assert body["status"] == "active"
        assert body["next_occurrence"] == FUTURE
        assert body["account_name"]  # denormalized account name
        rule_id = body["id"]

        listed = await client.get("/api/web/v1/recurring-rules")
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1

        got = await client.get(f"/api/web/v1/recurring-rules/{rule_id}")
        assert got.status_code == 200
        assert got.json()["id"] == rule_id

        updated = await client.patch(
            f"/api/web/v1/recurring-rules/{rule_id}",
            headers=headers,
            json={"amount": "4000", "category": "房租2"},
        )
        assert updated.status_code == 200
        assert updated.json()["amount"] == "4000.00"
        assert updated.json()["category"] == "房租2"

        paused = await client.post(f"/api/web/v1/recurring-rules/{rule_id}/pause", headers=headers)
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        resumed = await client.post(
            f"/api/web/v1/recurring-rules/{rule_id}/resume", headers=headers
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"

        skipped = await client.post(f"/api/web/v1/recurring-rules/{rule_id}/skip", headers=headers)
        assert skipped.status_code == 200
        assert skipped.json()["next_occurrence"] > FUTURE

        disabled = await client.post(
            f"/api/web/v1/recurring-rules/{rule_id}/disable", headers=headers
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"


async def test_recurring_rules_validation_and_ledger_isolation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    account_a = await _default_account_id(factory, "ou_recur_web_a")
    await _default_account_id(factory, "ou_recur_web_b")
    client_a, csrf_a = await _client(factory, "ou_recur_web_a")
    client_b, _ = await _client(factory, "ou_recur_web_b")
    headers_a = {"X-CSRF-Token": csrf_a}
    async with client_a, client_b:
        # Ledger B sees nothing from ledger A.
        listed_b = await client_b.get("/api/web/v1/recurring-rules")
        assert listed_b.status_code == 200
        assert listed_b.json()["items"] == []

        # Past next_occurrence → 422.
        bad = await client_a.post(
            "/api/web/v1/recurring-rules",
            headers=headers_a,
            json={**_create_payload(account_a), "next_occurrence": "2000-01-01"},
        )
        assert bad.status_code == 422

        # Missing required fields → 422.
        invalid = await client_a.post(
            "/api/web/v1/recurring-rules",
            headers=headers_a,
            json={"amount": "100"},
        )
        assert invalid.status_code == 422

        # Cross-ledger account reference → 422.
        cross_ledger_account = await _default_account_id(factory, "ou_recur_web_b")
        cross = await client_a.post(
            "/api/web/v1/recurring-rules",
            headers=headers_a,
            json=_create_payload(cross_ledger_account),
        )
        assert cross.status_code == 422

        # Unknown rule id → 404 for both users.
        missing = await client_a.get(
            "/api/web/v1/recurring-rules/00000000-0000-0000-0000-000000000000"
        )
        assert missing.status_code == 404


async def test_recurring_rule_cannot_access_other_ledger_rule(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    account_a = await _default_account_id(factory, "ou_recur_iso_a")
    await _default_account_id(factory, "ou_recur_iso_b")
    client_a, csrf_a = await _client(factory, "ou_recur_iso_a")
    client_b, csrf_b = await _client(factory, "ou_recur_iso_b")
    async with client_a, client_b:
        created = await client_a.post(
            "/api/web/v1/recurring-rules",
            headers={"X-CSRF-Token": csrf_a},
            json=_create_payload(account_a),
        )
        rule_id = created.json()["id"]

        # Ledger B cannot read, mutate, pause, or skip ledger A's rule.
        assert (await client_b.get(f"/api/web/v1/recurring-rules/{rule_id}")).status_code == 404
        for action in ("pause", "resume", "skip", "disable"):
            response = await client_b.post(
                f"/api/web/v1/recurring-rules/{rule_id}/{action}",
                headers={"X-CSRF-Token": csrf_b},
            )
            assert response.status_code == 404, action
        patch = await client_b.patch(
            f"/api/web/v1/recurring-rules/{rule_id}",
            headers={"X-CSRF-Token": csrf_b},
            json={"amount": "9999"},
        )
        assert patch.status_code == 404
