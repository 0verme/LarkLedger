"""P38 — First-party Web Client on real PostgreSQL (P38 §63).

Journey-level regression for the Web client's core bookkeeping lifecycle,
exercised through the actual ``/api/web/v1`` ASGI routes (Human Session +
CSRF + Idempotency-Key), never a repository shortcut:

    create → idempotency replay (still 1 row) → update → delete → restore
    ledger isolation
    household shared ledger + private account isolation
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.models import LedgerEntry
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.web_api import _auth_service, router

pytestmark = pytest.mark.postgres


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="integration-only-secret-long-enough-123456",
        dashboard_cookie_secure=False,
        dashboard_admin_open_ids="",
        lark_app_id="cli_test",
        lark_app_secret="test-secret",
        currency="CNY",
        timezone="Asia/Shanghai",
    )


async def _pg_client(
    factory: async_sessionmaker[AsyncSession],
    user: str,
) -> tuple[httpx.AsyncClient, str]:
    auth = DashboardAuthService(_settings(), factory)
    created = await auth.create_session({"open_id": user, "name": user, "avatar_url": ""})
    app = FastAPI()
    app.state.settings = _settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created.csrf_token


def _csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "amount": "28.00",
        "direction": "expense",
        "category": "餐饮",
        "note": "午餐",
        "occurred_at": "2026-08-10T04:30:00+08:00",
        "account_id": None,
    }
    body.update(overrides)
    return body


async def _count(factory: async_sessionmaker[AsyncSession], ledger_id: uuid.UUID) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.ledger_id == ledger_id)
            )
            or 0
        )


async def test_web_create_update_delete_restore_lifecycle_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _pg_client(postgres_session_factory, "ou_pg_a")
    try:
        current = await client.get("/api/web/v1/ledgers/current")
        ledger_id = uuid.UUID(current.json()["id"])
        created = await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "pg-lifecycle"},
            json=_body(note="PG 午餐"),
        )
        assert created.status_code == 201
        entry = created.json()["entry"]
        assert entry["amount"] == "28.00"
        assert entry["direction"] == "expense"

        # Update 28 → 30 (Journey A).
        updated = await client.patch(
            f"/api/web/v1/entries/{entry['short_id']}",
            headers=_csrf(csrf_token),
            json={
                "expected_updated_at": entry["updated_at"],
                "amount": "30.00",
                "direction": "expense",
                "category": "餐饮",
                "note": "午餐涨价了",
                "account_id": None,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["entry"]["amount"] == "30.00"

        # Delete → soft-deleted, then restore.
        deleted = await client.request(
            "DELETE",
            f"/api/web/v1/entries/{entry['short_id']}",
            headers=_csrf(csrf_token),
            json={"expected_updated_at": updated.json()["entry"]["updated_at"]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["entry"]["deleted_at"] is not None
        restored = await client.post(
            f"/api/web/v1/entries/{entry['short_id']}/restore",
            headers=_csrf(csrf_token),
            json={"expected_updated_at": deleted.json()["entry"]["updated_at"]},
        )
        assert restored.status_code == 200
        assert restored.json()["entry"]["deleted_at"] is None
        assert await _count(postgres_session_factory, ledger_id) == 1
    finally:
        await client.aclose()


async def test_web_idempotency_replay_keeps_one_row_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Journey D — a retried submit with the same Idempotency-Key must replay
    the stored response; ledger_entries stays at exactly 1."""
    client, csrf_token = await _pg_client(postgres_session_factory, "ou_pg_b")
    try:
        current = await client.get("/api/web/v1/ledgers/current")
        ledger_id = uuid.UUID(current.json()["id"])
        headers = {**_csrf(csrf_token), "Idempotency-Key": "pg-replay-key"}
        first = await client.post("/api/web/v1/entries", headers=headers, json=_body())
        assert first.status_code == 201
        replay = await client.post("/api/web/v1/entries", headers=headers, json=_body())
        assert replay.status_code == 201
        assert replay.json()["entry"]["short_id"] == first.json()["entry"]["short_id"]
        assert await _count(postgres_session_factory, ledger_id) == 1
    finally:
        await client.aclose()


async def test_web_ledger_isolation_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A's transactions never leak to B; guessing A's entry short id is 404."""
    owner, owner_csrf = await _pg_client(postgres_session_factory, "ou_pg_owner")
    try:
        created = await owner.post(
            "/api/web/v1/entries",
            headers={**_csrf(owner_csrf), "Idempotency-Key": "pg-isolation"},
            json=_body(note="私有流水"),
        )
        assert created.status_code == 201
        short_id = created.json()["entry"]["short_id"]
    finally:
        await owner.aclose()

    outsider, _outsider_csrf = await _pg_client(postgres_session_factory, "ou_pg_x")
    try:
        listed = await outsider.get("/api/web/v1/entries")
        assert listed.status_code == 200
        assert all(item["short_id"] != short_id for item in listed.json()["items"])
        direct = await outsider.get(f"/api/web/v1/entries/{short_id}")
        assert direct.status_code == 404
    finally:
        await outsider.aclose()


async def test_web_household_shared_ledger_and_private_isolation_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Journey B + C on PostgreSQL: both members book into the shared ledger;
    A's private account is invisible and unreachable for B."""
    async with postgres_session_factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_pg_h_a", display_name="A")
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_pg_h_b", display_name="B")
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "PG 家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_pg_h_b")
        await manager.accept(member.actor_user_id, invitation.public_id)
        await session.commit()
        home_ledger_id = home.ledger.id
        owner_personal_id = owner.ledger_id

    owner_client, owner_csrf = await _pg_client(postgres_session_factory, "ou_pg_h_a")
    try:
        selected = await owner_client.post(
            f"/api/web/v1/ledgers/{home_ledger_id}/select",
            headers=_csrf(owner_csrf),
        )
        assert selected.status_code == 200
        created = await owner_client.post(
            "/api/web/v1/entries",
            headers={**_csrf(owner_csrf), "Idempotency-Key": "pg-household-shared"},
            json=_body(note="家庭支出"),
        )
        assert created.status_code == 201
        shared_short_id = created.json()["entry"]["short_id"]
        private = await owner_client.post(
            "/api/web/v1/accounts",
            headers=_csrf(owner_csrf),
            json={
                "name": "PG 私密账户",
                "type": "asset",
                "opening_balance": "0.00",
                "visibility": "private",
            },
        )
        assert private.status_code == 201
        private_account_id = private.json()["id"]
    finally:
        await owner_client.aclose()

    member_client, member_csrf = await _pg_client(postgres_session_factory, "ou_pg_h_b")
    try:
        member_select = await member_client.post(
            f"/api/web/v1/ledgers/{home_ledger_id}/select",
            headers=_csrf(member_csrf),
        )
        assert member_select.status_code == 200
        listed = await member_client.get("/api/web/v1/entries")
        assert any(item["short_id"] == shared_short_id for item in listed.json()["items"])
        accounts = await member_client.get("/api/web/v1/accounts")
        assert private_account_id not in {item["id"] for item in accounts.json()["items"]}
        direct = await member_client.get(f"/api/web/v1/accounts/{private_account_id}")
        assert direct.status_code == 404
        # Cross-ledger: B cannot select A's personal ledger.
        cross = await member_client.post(
            f"/api/web/v1/ledgers/{owner_personal_id}/select",
            headers=_csrf(member_csrf),
        )
        assert cross.status_code == 404
    finally:
        await member_client.aclose()
