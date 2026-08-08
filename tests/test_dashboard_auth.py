from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, DashboardSession
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthError,
    DashboardAuthService,
    safe_next_path,
)
from lark_ledger.web_api import _auth_service, router


def dashboard_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "dashboard_enabled": True,
        "dashboard_base_url": "http://ledger.test",
        "dashboard_session_secret": "test-only-secret-that-is-long-enough-123456",
        "dashboard_cookie_secure": False,
        "dashboard_admin_open_ids": "ou_admin, ou_second",
        "lark_app_id": "cli_test",
        "lark_app_secret": "app-secret",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def dashboard_factory() -> async_sessionmaker[AsyncSession]:
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


def test_dashboard_requires_strong_secret_and_secure_origin() -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        dashboard_settings(dashboard_session_secret="short")
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        dashboard_settings(dashboard_session_secret="a" * 32)
    with pytest.raises(ValueError, match="https"):
        dashboard_settings(dashboard_cookie_secure=True)
    assert Settings(_env_file=None, dashboard_enabled=False).dashboard_enabled is False


def test_redirect_allowlist_only_accepts_same_origin_paths() -> None:
    assert safe_next_path("/entries?days=30") == "/entries?days=30"
    for invalid in ("https://evil.test", "//evil.test/path", "entries"):
        with pytest.raises(DashboardAuthError, match="跳转地址"):
            safe_next_path(invalid)


def test_oauth_state_pkce_and_tampering(dashboard_factory: Any) -> None:
    service = DashboardAuthService(dashboard_settings(), dashboard_factory)
    request = service.begin_oauth("/entries")
    assert "code_challenge_method=S256" in request.authorize_url
    assert "scope=auth%3Auser.id%3Aread" in request.authorize_url
    state = httpx.URL(request.authorize_url).params["state"]
    verifier, next_path = service.complete_oauth_state(request.state_cookie, state)
    assert len(verifier) >= 64
    assert next_path == "/entries"
    with pytest.raises(DashboardAuthError, match="state 校验"):
        service.complete_oauth_state(request.state_cookie, "wrong")
    with pytest.raises(DashboardAuthError, match="state 已失效"):
        service.complete_oauth_state("broken" + request.state_cookie[6:], state)


async def test_oauth_exchange_returns_identity_without_exposing_token(
    dashboard_factory: async_sessionmaker[AsyncSession],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            body = request.content.decode()
            assert '"code":"one-time-code"' in body
            assert '"code_verifier":"verifier"' in body
            return httpx.Response(200, json={"code": 0, "access_token": "secret-user-token"})
        assert request.headers["Authorization"] == "Bearer secret-user-token"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"open_id": "ou_user", "name": "飞飞"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DashboardAuthService(dashboard_settings(), dashboard_factory, client=client)
        identity = await service.exchange_identity("one-time-code", "verifier")
    assert identity == {"open_id": "ou_user", "name": "飞飞", "avatar_url": ""}
    assert "token" not in identity


async def test_session_user_admin_csrf_expiry_and_logout(
    dashboard_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(dashboard_settings(), dashboard_factory)
    user = await service.create_session({"open_id": "ou_user", "name": "用户", "avatar_url": ""})
    admin = await service.create_session(
        {"open_id": "ou_admin", "name": "管理员", "avatar_url": ""}
    )
    assert (await service.authenticate(user.session_token)).role == "USER"
    assert (await service.authenticate(admin.session_token)).role == "ADMIN"
    assert (
        await service.verify_csrf(user.session_token, user.csrf_token, user.csrf_token)
    ).user_open_id == "ou_user"
    with pytest.raises(DashboardAuthError, match="CSRF"):
        await service.verify_csrf(user.session_token, user.csrf_token, "wrong")

    await service.revoke(user.session_token)
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(user.session_token)

    async with dashboard_factory() as session:
        row = await session.scalar(
            select(DashboardSession).where(DashboardSession.user_open_id == "ou_admin")
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(admin.session_token)


async def test_me_logout_and_csrf_http_boundary(
    dashboard_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = dashboard_settings()
    service = DashboardAuthService(settings, dashboard_factory)
    created = await service.create_session({"open_id": "ou_user", "name": "小飞", "avatar_url": ""})
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = dashboard_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ledger.test") as client:
        client.cookies.set(SESSION_COOKIE, created.session_token)
        client.cookies.set(CSRF_COOKIE, created.csrf_token)
        me = await client.get("/api/web/v1/me")
        assert me.status_code == 200
        assert me.json()["open_id"] == "ou_user"
        rejected = await client.post("/api/web/v1/auth/logout")
        assert rejected.status_code == 403
        logged_out = await client.post(
            "/api/web/v1/auth/logout",
            headers={"X-CSRF-Token": created.csrf_token},
        )
        assert logged_out.status_code == 204
        assert (await client.get("/api/web/v1/me")).status_code == 401


async def test_login_boundary_sets_short_lived_http_only_state_cookie(
    dashboard_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = dashboard_settings()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = dashboard_factory
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://ledger.test", follow_redirects=False
    ) as client:
        response = await client.get("/api/web/v1/auth/login?next=/entries")
        assert response.status_code == 302
        assert response.headers["location"].startswith(
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
        )
        cookie = response.headers["set-cookie"]
        assert "lark_ledger_oauth=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        invalid = await client.get(
            "/api/web/v1/auth/login",
            params={"next": "https://evil.test"},
        )
        assert invalid.status_code == 400
