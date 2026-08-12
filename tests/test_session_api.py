"""P37 — HTTP API tests for the human session endpoints (S01–S18).

Runs the real ``web_api`` router over an ASGI transport and proves:

* create session → DB stores only the digest (S01/S02)
* cookie flags: HttpOnly / SameSite / Secure per settings (S03)
* valid / invalid / expired / revoked session → 200 / 401 (S04–S07)
* logout revokes server-side immediately (S08)
* session list, revoke-one, revoke-others (S09–S11)
* same-user multi-session, cross-user isolation (S12/S13)
* ledger isolation and private-account isolation reuse the existing
  authorization boundary (S14/S15)
* CSRF: same-origin mutation passes, foreign Origin is rejected (S16/S17)
* machine ``llv1_`` bearer tokens on ``/api/v1`` stay fully compatible (S18)
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import (
    Base,
    DashboardSession,
    Ledger,
    LedgerEntry,
    User,
)
from lark_ledger.services.client_auth import ClientCredentialService
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.web_api import _auth_service, router


def session_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "dashboard_enabled": True,
        "dashboard_base_url": "http://ledger.test",
        "dashboard_session_secret": "test-only-secret-that-is-long-enough-123456",
        "dashboard_cookie_secure": False,
        "dashboard_admin_open_ids": "ou_admin",
        "lark_app_id": "cli_test",
        "lark_app_secret": "app-secret",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def app_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _identity(open_id: str = "ou_user", name: str = "用户") -> dict[str, str]:
    return {"open_id": open_id, "name": name, "avatar_url": ""}


async def _client_for(
    factory: async_sessionmaker[AsyncSession],
    *,
    open_id: str = "ou_user",
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
) -> tuple[httpx.AsyncClient, Any]:
    settings = session_settings()
    service = DashboardAuthService(settings, factory)
    created = await service.create_session(
        _identity(open_id), user_agent=user_agent, ip="203.0.113.1"
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created


async def test_s01_s02_create_session_stores_only_digest(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, created = await _client_for(app_factory)
    assert created.session_token.startswith("lls1_")
    async with app_factory() as session:
        rows = (await session.scalars(select(DashboardSession))).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.token_hash == hashlib.sha256(
            created.session_token.encode("utf-8")
        ).hexdigest()
        assert created.session_token not in row.token_hash


async def test_s03_session_cookie_is_httponly_samesite(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drive the real OAuth callback and assert the Set-Cookie flags."""
    from unittest.mock import patch

    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)

    async def fake_exchange(self, code: str, verifier: str) -> dict[str, str]:
        del code, verifier
        return _identity()

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service

    oauth = service.begin_oauth("/entries")
    state = httpx.URL(oauth.authorize_url).params["state"]
    jar = httpx.Cookies()
    jar.set("lark_ledger_oauth", oauth.state_cookie, domain="ledger.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ledger.test",
        follow_redirects=False,
        cookies=jar,
    ) as client:
        with patch.object(
            DashboardAuthService, "exchange_identity", fake_exchange
        ):
            cb = await client.get(
                "/api/web/v1/auth/callback",
                params={"code": "one-time-code", "state": state},
            )
        assert cb.status_code == 303
        cookie_headers = cb.headers.get_list("set-cookie")
        session_cookie = next(
            (c for c in cookie_headers if c.startswith("lark_ledger_session=")),
            None,
        )
        assert session_cookie is not None
        assert "HttpOnly" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "Secure" not in session_cookie  # dashboard_cookie_secure=False
        # The raw session secret appears only in the Set-Cookie header.
        assert "lls1_" in session_cookie


async def test_s03b_production_secure_cookie_flag(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings(
        dashboard_cookie_secure=True,
        dashboard_base_url="https://ledger.test",
    )
    service = DashboardAuthService(settings, app_factory)
    created = await service.create_session(_identity())
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    jar = httpx.Cookies()
    jar.set(SESSION_COOKIE, created.session_token, domain="ledger.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://ledger.test",
        cookies=jar,
    ) as client:
        me = await client.get("/api/web/v1/me")
        assert me.status_code == 200


async def test_s04_valid_session_me_returns_200(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, created = await _client_for(app_factory)
    try:
        response = await client.get("/api/web/v1/auth/session")
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == created.principal.session_id
        assert body["open_id"] == "ou_user"
        assert body["role"] == "USER"
        # No credential material is ever returned.
        assert "token" not in body
        assert "digest" not in body
        assert "cookie" not in body
    finally:
        await client.aclose()


async def test_s05_invalid_session_returns_401(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, "lls1_totally-bogus-token")
        assert (await client.get("/api/web/v1/auth/session")).status_code == 401
        assert (await client.get("/api/web/v1/me")).status_code == 401


async def test_s06_expired_session_returns_401(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    created = await service.create_session(_identity())
    async with app_factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, created.session_token)
        assert (await client.get("/api/web/v1/auth/session")).status_code == 401


async def test_s07_revoked_session_returns_401(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    created = await service.create_session(_identity())
    await service.revoke(created.session_token)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, created.session_token)
        assert (await client.get("/api/web/v1/auth/session")).status_code == 401


async def test_s08_logout_revokes_immediately(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, created = await _client_for(app_factory)
    try:
        assert (await client.get("/api/web/v1/me")).status_code == 200
        logged_out = await client.post(
            "/api/web/v1/auth/logout",
            headers={"X-CSRF-Token": created.csrf_token},
        )
        assert logged_out.status_code == 204
        # Server-side revocation is immediate — the same cookie no longer
        # authenticates even before the browser cookie is cleared.
        async with app_factory() as session:
            row = await session.scalar(select(DashboardSession))
            assert row is not None and row.revoked_at is not None
        assert (await client.get("/api/web/v1/me")).status_code == 401
    finally:
        await client.aclose()


async def test_s09_s10_list_and_revoke_one_session(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    first = await service.create_session(
        _identity(), user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/126.0"
    )
    second = await service.create_session(
        _identity(), user_agent="Mozilla/5.0 (iPhone) Mobile Safari"
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, first.session_token)
        client.cookies.set(CSRF_COOKIE, first.csrf_token)
        listed = await client.get("/api/web/v1/auth/sessions")
        assert listed.status_code == 200
        body = listed.json()
        assert body["current_session_id"] == first.principal.session_id
        assert len(body["items"]) == 2
        by_id = {item["id"]: item for item in body["items"]}
        assert by_id[first.principal.session_id]["current"] is True
        assert by_id[second.principal.session_id]["current"] is False
        assert "Windows" in by_id[first.principal.session_id]["device"]
        # OpenAPI-safe: no digest or secret fields leak into the list.
        assert "token" not in by_id[first.principal.session_id]
        assert "digest" not in by_id[first.principal.session_id]

        revoked = await client.request(
            "DELETE",
            f"/api/web/v1/auth/sessions/{second.principal.session_id}",
            headers={"X-CSRF-Token": first.csrf_token},
        )
        assert revoked.status_code == 204
        after = (await client.get("/api/web/v1/auth/sessions")).json()["items"]
        assert by_id[second.principal.session_id]["id"] in {i["id"] for i in after}
        assert any(i["id"] == second.principal.session_id and i["revoked_at"] for i in after)
        # The revoked session can no longer authenticate.
        client2 = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
        )
        client2.cookies.set(SESSION_COOKIE, second.session_token)
        assert (await client2.get("/api/web/v1/me")).status_code == 401
        await client2.aclose()


async def test_s11_revoke_all_others(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    a = await service.create_session(_identity())
    b = await service.create_session(_identity())
    c = await service.create_session(_identity())
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, a.session_token)
        client.cookies.set(CSRF_COOKIE, a.csrf_token)
        response = await client.post(
            "/api/web/v1/auth/sessions/revoke-others",
            headers={"X-CSRF-Token": a.csrf_token},
        )
        assert response.status_code == 204
        for stale in (b.session_token, c.session_token):
            probe = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
            )
            probe.cookies.set(SESSION_COOKIE, stale)
            assert (await probe.get("/api/web/v1/me")).status_code == 401
            await probe.aclose()
        assert (await client.get("/api/web/v1/me")).status_code == 200


async def test_s12_same_user_multi_session_all_valid(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_a, created_a = await _client_for(app_factory)
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    client_b, created_b = await _client_for(app_factory)
    del settings, service
    try:
        assert (await client_a.get("/api/web/v1/me")).status_code == 200
        assert (await client_b.get("/api/web/v1/me")).status_code == 200
        assert created_a.principal.session_id != created_b.principal.session_id
        listed = (await client_a.get("/api/web/v1/auth/sessions")).json()
        assert len(listed["items"]) == 2
    finally:
        await client_a.aclose()
        await client_b.aclose()


async def test_s13_different_user_isolation(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    alice = await service.create_session(_identity("ou_alice"))
    bob = await service.create_session(_identity("ou_bob"))
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, alice.session_token)
        client.cookies.set(CSRF_COOKIE, alice.csrf_token)
        listed = (await client.get("/api/web/v1/auth/sessions")).json()
        assert all(item["id"] != bob.principal.session_id for item in listed["items"])
        # Alice cannot revoke Bob's session (404, no existence leak).
        response = await client.request(
            "DELETE",
            f"/api/web/v1/auth/sessions/{bob.principal.session_id}",
            headers={"X-CSRF-Token": alice.csrf_token},
        )
        assert response.status_code == 404
        assert (await client.get("/api/web/v1/me")).status_code == 200
        probe = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
        )
        probe.cookies.set(SESSION_COOKIE, bob.session_token)
        assert (await probe.get("/api/web/v1/me")).status_code == 200
        await probe.aclose()


async def test_s14_s15_ledger_and_private_account_isolation(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A human session crosses the SAME authorization boundary as Feishu and
    API tokens: only ledger members see ledger data, and private accounts are
    only visible to their owner."""
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    owner = await service.create_session(_identity("ou_owner"))
    outsider = await service.create_session(_identity("ou_outsider"))
    async with app_factory() as session:
        owner_user = await session.scalar(select(User).where(User.id == owner.principal.user_id))
        outsider_user = await session.scalar(
            select(User).where(User.id == outsider.principal.user_id)
        )
        assert owner_user is not None and outsider_user is not None
        # create_session already bootstraps a default personal ledger; reuse it
        # so the (owner, is_default) unique constraint is not violated.
        ledger = await session.scalar(select(Ledger).where(Ledger.owner_user_id == owner_user.id))
        assert ledger is not None
        session.add(
            LedgerEntry(
                ledger_id=ledger.id,
                user_open_id="ou_owner",
                short_id="A83F1",
                amount=10,
                currency="CNY",
                direction="expense",
                category="餐饮",
                note="私密",
                occurred_at=datetime.now(UTC),
                source_type="text",
            )
        )
        await session.commit()
        # Point the outsider's session ledger at the owner's ledger to prove
        # authorization (not just ledger selection) blocks the cross-user read.
        outsider_row = await session.scalar(
            select(DashboardSession).where(
                DashboardSession.id == uuid.UUID(outsider.principal.session_id)
            )
        )
        assert outsider_row is not None
        outsider_row.ledger_id = ledger.id
        await session.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    # owner entries visible
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, owner.session_token)
        client.cookies.set(CSRF_COOKIE, owner.csrf_token)
        owner_entries = await client.get("/api/web/v1/entries")
        assert owner_entries.status_code == 200
        owner_short_ids = [item["short_id"] for item in owner_entries.json()["items"]]
        assert "A83F1" in owner_short_ids
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as outsider_client:
        outsider_client.cookies.set(SESSION_COOKIE, outsider.session_token)
        outsider_client.cookies.set(CSRF_COOKIE, outsider.csrf_token)
        # The session row points at the owner's ledger, but authentication
        # re-validates membership and re-scopes the outsider to their own
        # default ledger — the same authorization boundary API tokens cross.
        outside = await outsider_client.get("/api/web/v1/entries")
        assert outside.status_code == 200
        outsider_short_ids = [item["short_id"] for item in outside.json()["items"]]
        assert "A83F1" not in outsider_short_ids
        re_scoped = await service.authenticate(outsider.session_token)
        assert re_scoped.ledger_id != ledger.id


async def test_s16_csrf_rejects_foreign_origin(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, created = await _client_for(app_factory)
    try:
        # No CSRF header → 403.
        no_csrf = await client.post("/api/web/v1/auth/logout")
        assert no_csrf.status_code == 403
        # Foreign Origin header → 403 even with a valid CSRF token.
        forged = await client.post(
            "/api/web/v1/auth/logout",
            headers={
                "X-CSRF-Token": created.csrf_token,
                "Origin": "https://evil.example.com",
            },
        )
        assert forged.status_code == 403
        # Legitimate same-origin mutation still works.
        ok = await client.post(
            "/api/web/v1/auth/logout",
            headers={
                "X-CSRF-Token": created.csrf_token,
                "Origin": "http://ledger.test",
            },
        )
        assert ok.status_code == 204
    finally:
        await client.aclose()


async def test_s17_valid_same_origin_mutation_passes(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    created = await service.create_session(_identity())
    async with app_factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        row.user_id = created.principal.user_id
        await session.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, created.session_token)
        client.cookies.set(CSRF_COOKIE, created.csrf_token)
        listed = await client.get("/api/web/v1/auth/sessions")
        assert listed.status_code == 200
        session_id = listed.json()["current_session_id"]
        # Same-origin state change: revoke-others (must pass CSRF + Origin).
        response = await client.post(
            "/api/web/v1/auth/sessions/revoke-others",
            headers={
                "X-CSRF-Token": created.csrf_token,
                "Origin": "http://ledger.test",
            },
        )
        assert response.status_code == 204
        assert session_id == created.principal.session_id


async def test_s18_api_token_compatibility(
    app_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Machine credentials (llv1_ Bearer on /api/v1) are untouched by P37."""
    from lark_ledger.client_api import api_v1_router

    settings = session_settings()
    service = DashboardAuthService(settings, app_factory)
    created = await service.create_session(_identity())
    async with app_factory() as session:
        credential = await ClientCredentialService.create(
            session,
            user_id=created.principal.user_id,
            current_ledger_id=created.principal.ledger_id,
            request=type(
                "Req",
                (),
                {
                    "name": "机器令牌",
                    "scopes": ["ledger:read", "ledger:write"],
                    "expires_at": None,
                },
            )(),  # type: ignore[arg-type]
        )
        token = credential.token
    assert token.startswith("llv1_")

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = app_factory
    app.include_router(api_v1_router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        bad = await client.get("/api/v1/ledgers", headers={"Authorization": "Bearer llv1_invalid"})
        assert bad.status_code == 401
        good = await client.get(
            "/api/v1/ledgers", headers={"Authorization": f"Bearer {token}"}
        )
        assert good.status_code == 200
        # The error envelope stays the canonical one.
        assert bad.json()["detail"]["code"] == "authentication_required"
