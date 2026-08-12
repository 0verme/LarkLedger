"""P37 — secret leakage audit (unit level).

Proves the operational contract:

* a real session secret never reaches a logger (reason=not_found/revoked/expired
  log lines contain no token material);
* ``lls1_`` never appears in log output during a full auth flow;
* raw secrets are not written to disk by the auth service;
* the session service source performs no credential logging.

The test intentionally drives the REAL ``DashboardAuthService`` (including its
log statements) and captures stdlib logging output.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, DashboardSession
from lark_ledger.services.dashboard_auth import (
    SESSION_SECRET_PREFIX,
    DashboardAuthError,
    DashboardAuthService,
)


def _settings(**overrides: Any) -> Settings:
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
async def factory() -> async_sessionmaker[AsyncSession]:
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


@pytest.fixture
def captured_logs() -> io.StringIO:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("lark_ledger.services.dashboard_auth")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    yield stream
    logger.removeHandler(handler)
    logger.propagate = True


async def test_no_session_secret_in_logs_during_full_flow(
    factory: async_sessionmaker[AsyncSession],
    captured_logs: io.StringIO,
) -> None:
    service = DashboardAuthService(_settings(), factory)
    created = await service.create_session({"open_id": "ou_user", "name": "小飞", "avatar_url": ""})
    raw_secret = created.session_token
    csrf_secret = created.csrf_token

    # Successful auth should log nothing at all (or at least nothing secret).
    await service.authenticate(created.session_token)
    # Failed auth logs a reason but never the token.
    with pytest.raises(DashboardAuthError):
        await service.authenticate("lls1_totally-wrong-secret-value-1234567890")
    with pytest.raises(DashboardAuthError):
        await service.authenticate(None)
    await service.revoke(created.session_token)
    with pytest.raises(DashboardAuthError):
        await service.authenticate(raw_secret)

    output = captured_logs.getvalue()
    assert raw_secret not in output
    assert csrf_secret not in output
    assert SESSION_SECRET_PREFIX not in output
    assert "lls1_" not in output


async def test_no_raw_secret_persisted_anywhere(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(_settings(), factory)
    created = await service.create_session({"open_id": "ou_user", "name": "小飞", "avatar_url": ""})
    async with factory() as session:
        rows = (await session.scalars(select(DashboardSession))).all()
        assert len(rows) == 1
        row = rows[0]
        assert created.session_token not in row.token_hash
        assert created.session_token not in row.csrf_hash
        assert created.csrf_token not in row.token_hash
        assert created.csrf_token not in row.csrf_hash


def test_session_service_source_never_logs_credentials() -> None:
    import pathlib

    source = pathlib.Path("src/lark_ledger/services/dashboard_auth.py").read_text(
        encoding="utf-8"
    )
    for line in source.splitlines():
        stripped = line.strip()
        if "logger." not in stripped:
            continue
        assert "token" not in stripped.lower(), f"credential logging: {stripped}"
        assert "cookie" not in stripped.lower(), f"credential logging: {stripped}"
        assert "secret" not in stripped.lower(), f"credential logging: {stripped}"


def test_web_api_source_never_logs_request_headers_or_cookies() -> None:
    import pathlib

    source = pathlib.Path("src/lark_ledger/web_api.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if "logger." not in stripped:
            continue
        assert "headers" not in stripped.lower(), f"header logging: {stripped}"
        assert "cookies" not in stripped.lower(), f"cookie logging: {stripped}"
        assert "authorization" not in stripped.lower(), f"auth logging: {stripped}"
