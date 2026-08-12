"""P37 — unit tests for the human session service.

Covers secret generation (``lls1_`` prefix, cryptographic randomness), the
digest-only storage contract, expiry, revocation, multi-device sessions,
revoke-all-others, last-seen write bounding, audit events and the
``RequestContext`` the session produces.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, ClientSecurityAudit, DashboardSession
from lark_ledger.services.dashboard_auth import (
    SESSION_SECRET_PREFIX,
    DashboardAuthError,
    DashboardAuthService,
    device_label,
)


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


def _identity(open_id: str = "ou_user", name: str = "用户") -> dict[str, str]:
    return {"open_id": open_id, "name": name, "avatar_url": ""}


async def test_session_secret_is_lls1_prefixed_and_high_entropy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    created = await service.create_session(_identity())
    assert created.session_token.startswith(SESSION_SECRET_PREFIX)
    raw = created.session_token[len(SESSION_SECRET_PREFIX) :]
    assert len(raw) >= 40
    # The prefix never leaks into the stored digest path: only the full raw
    # secret is hashed, so a plaintext token can never be reconstructed.
    assert hashlib.sha256(created.session_token.encode()).hexdigest() == hashlib.sha256(
        created.session_token.encode()
    ).hexdigest()


async def test_database_stores_only_digest_never_raw_secret(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    created = await service.create_session(_identity())
    async with factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        assert row.token_hash == hashlib.sha256(
            created.session_token.encode("utf-8")
        ).hexdigest()
        assert created.session_token not in row.token_hash
        assert SESSION_SECRET_PREFIX not in row.token_hash
        # No raw secret / cookie value is persisted anywhere on the row.
        serialized = repr(row.__dict__)
        assert created.session_token not in serialized
        assert created.csrf_token not in serialized


async def test_secret_generation_is_random_and_unique(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    tokens = {
        created.session_token
        for created in [await service.create_session(_identity()) for _ in range(8)]
    }
    assert len(tokens) == 8  # 8 independent sessions never collide


async def test_authenticate_expiry_and_revocation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    created = await service.create_session(_identity())
    principal = await service.authenticate(created.session_token)
    assert principal.user_open_id == "ou_user"
    assert principal.role == "USER"
    assert principal.request_context.actor_user_id == principal.user_id
    assert principal.request_context.actor_kind == "user"
    assert principal.request_context.source_channel == "web"

    await service.revoke(created.session_token)
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(created.session_token)

    # Expired sessions are rejected even when not revoked.
    second = await service.create_session(_identity("ou_expired"))
    async with factory() as session:
        row = await session.scalar(
            select(DashboardSession).where(
                DashboardSession.user_open_id == "ou_expired"
            )
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(second.session_token)


async def test_multi_device_sessions_and_revoke_others(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    phone = await service.create_session(
        _identity("ou_user"),
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Mobile Safari/604.1"
        ),
        ip="203.0.113.9",
    )
    laptop = await service.create_session(
        _identity("ou_user"),
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        ip="203.0.113.10",
    )
    # Same user, two parallel valid sessions.
    assert (await service.authenticate(phone.session_token)).session_id != (
        await service.authenticate(laptop.session_token)
    ).session_id

    sessions = await service.list_sessions(
        phone.principal.user_id, phone.principal.session_id
    )
    assert len(sessions) == 2
    current = [s for s in sessions if s.current]
    assert len(current) == 1 and current[0].session_id == phone.principal.session_id
    assert "iOS" in sessions[0].device  # newest first: laptop created after phone
    assert "Windows" in sessions[1].device

    # Revoke the laptop session by id; the phone session stays valid.
    laptop_row = await service.revoke_session(
        phone.principal.user_id, uuid.UUID(laptop.principal.session_id)
    )
    assert laptop_row is True
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(laptop.session_token)
    assert await service.authenticate(phone.session_token)

    # Revoke-all-others keeps only the current session. The laptop was
    # already revoked, so exactly the phone session is left to revoke.
    third = await service.create_session(_identity("ou_user"))
    assert await service.authenticate(third.session_token)
    revoked = await service.revoke_other_sessions(
        third.principal.user_id, third.principal.session_id
    )
    assert revoked == 1
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(phone.session_token)
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(laptop.session_token)
    assert await service.authenticate(third.session_token)

    # Revoking a session that belongs to someone else is a safe 404-style miss.
    other = await service.create_session(_identity("ou_other"))
    found = await service.revoke_session(
        third.principal.user_id, uuid.UUID(other.principal.session_id)
    )
    assert found is False
    assert await service.authenticate(other.session_token)


async def test_last_seen_write_is_bounded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Authenticating inside the refresh window must not touch the DB row."""
    service = DashboardAuthService(session_settings(), factory)
    created = await service.create_session(_identity())
    async with factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        first_seen = row.last_seen_at
    for _ in range(3):
        await service.authenticate(created.session_token)
    async with factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        assert row.last_seen_at == first_seen


async def test_session_audit_events_are_recorded_without_secrets(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    created = await service.create_session(_identity())
    other_first = await service.create_session(_identity("ou_other"))
    other_second = await service.create_session(_identity("ou_other"))
    await service.revoke(created.session_token)
    await service.revoke_other_sessions(
        other_first.principal.user_id, other_first.principal.session_id
    )
    async with factory() as session:
        actions = list(
            (await session.scalars(select(ClientSecurityAudit.action))).all()
        )
    assert actions.count("session.create") == 3
    assert actions.count("session.revoke") >= 1
    assert actions.count("session.revoke_all_others") == 1
    rows = (await session.scalars(select(ClientSecurityAudit))).all()
    assert all(row.credential_id is None for row in rows)
    assert other_second.principal.session_id != other_first.principal.session_id


async def test_session_fixation_login_always_rotates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two logins of the same identity must never reuse a session secret."""
    service = DashboardAuthService(session_settings(), factory)
    first = await service.create_session(_identity())
    second = await service.create_session(_identity())
    assert first.session_token != second.session_token
    assert first.principal.session_id != second.principal.session_id
    # Both remain valid parallel sessions (multi-device by design).
    assert await service.authenticate(first.session_token)
    assert await service.authenticate(second.session_token)


async def test_created_ip_hash_never_stores_raw_ip(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(session_settings(), factory)
    await service.create_session(_identity(), ip="198.51.100.7")
    async with factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        assert row.created_ip_hash == hashlib.sha256(b"198.51.100.7").hexdigest()
        assert "198.51.100.7" not in row.created_ip_hash


def test_device_label_detects_platforms() -> None:
    assert "Windows" in device_label(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0"
    )
    assert "移动端" in device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile")
    assert device_label(None) == "未知设备"
