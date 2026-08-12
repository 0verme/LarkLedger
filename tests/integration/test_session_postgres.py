"""P37 — PostgreSQL integration tests for human sessions.

Verified against real PostgreSQL (never SQLite): digest-only storage, the
unique digest constraint, multi-device sessions, revocation, expiry, revoke
all others, and the Cleanup Worker's session retention sweep.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.models import ClientSecurityAudit, DashboardSession, User
from lark_ledger.services.cleanup import CleanupService, CleanupStore, RetentionPolicy
from lark_ledger.services.dashboard_auth import (
    SESSION_SECRET_PREFIX,
    DashboardAuthError,
    DashboardAuthService,
)

pytestmark = pytest.mark.postgres


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "dashboard_enabled": True,
        "dashboard_base_url": "http://ledger.test",
        "dashboard_session_secret": "integration-only-secret-long-enough-123456",
        "dashboard_cookie_secure": False,
        "dashboard_admin_open_ids": "ou_admin",
        "lark_app_id": "cli_test",
        "lark_app_secret": "test-secret",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _identity(open_id: str, name: str = "用户") -> dict[str, str]:
    return {"open_id": open_id, "name": name, "avatar_url": ""}


async def test_session_lifecycle_and_multi_device_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(_settings(), postgres_session_factory)
    phone = await service.create_session(
        _identity("ou_user"), user_agent="iPhone Mobile", ip="198.51.100.1"
    )
    laptop = await service.create_session(
        _identity("ou_user"), user_agent="Windows Chrome", ip="198.51.100.2"
    )
    # Two parallel sessions for one user are both valid.
    assert (await service.authenticate(phone.session_token)).user_open_id == "ou_user"
    assert (await service.authenticate(laptop.session_token)).user_open_id == "ou_user"

    # DB stores only digests, never raw secrets.
    async with postgres_session_factory() as session:
        rows = (await session.scalars(select(DashboardSession))).all()
        assert len(rows) == 2
        for row in rows:
            assert SESSION_SECRET_PREFIX not in row.token_hash
            assert row.user_agent is not None
            assert row.created_ip_hash is not None
            assert "198.51.100" not in row.created_ip_hash
        audits = (await session.scalars(select(ClientSecurityAudit))).all()
        assert len([a for a in audits if a.action == "session.create"]) == 2

    # Revoke one device; the other stays live.
    assert await service.revoke_session(
        phone.principal.user_id, uuid.UUID(laptop.principal.session_id)
    )
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(laptop.session_token)
    assert await service.authenticate(phone.session_token)

    # Revoke all others keeps only the acting session.
    third = await service.create_session(_identity("ou_user"))
    revoked = await service.revoke_other_sessions(
        third.principal.user_id, third.principal.session_id
    )
    assert revoked == 1  # laptop already revoked; phone revoked now
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(phone.session_token)
    assert await service.authenticate(third.session_token)


async def test_expired_session_rejected_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(_settings(), postgres_session_factory)
    created = await service.create_session(_identity("ou_user"))
    async with postgres_session_factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(created.session_token)


async def test_unique_session_digest_constraint_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(_settings(), postgres_session_factory)
    await service.create_session(_identity("ou_user"))
    async with postgres_session_factory() as session:
        original = await session.scalar(select(DashboardSession))
        assert original is not None
        duplicate = DashboardSession(
            token_hash=original.token_hash,
            csrf_hash=original.csrf_hash,
            user_open_id="ou_dup",
            user_id=original.user_id,
            ledger_id=original.ledger_id,
            display_name="重复",
            expires_at=datetime.now(UTC) + timedelta(days=1),
            last_seen_at=datetime.now(UTC),
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_session_cleanup_removes_only_retired_rows_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(_settings(), postgres_session_factory)
    live = await service.create_session(_identity("ou_live"))
    revoked = await service.create_session(_identity("ou_revoked"))
    expired = await service.create_session(_identity("ou_expired"))
    await service.revoke(revoked.session_token)
    old = datetime.now(UTC) - timedelta(days=400)
    async with postgres_session_factory() as session:
        # Age the revoked and expired rows far beyond the retention window.
        rows = (await session.scalars(select(DashboardSession))).all()
        for row in rows:
            if row.user_open_id in {"ou_revoked", "ou_expired"}:
                row.created_at = old
        # Expire one session outright.
        for row in rows:
            if row.user_open_id == "ou_expired":
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    store = CleanupStore(postgres_session_factory)
    service_clean = CleanupService(
        store, RetentionPolicy(session_retention_days=30), batch_size=10
    )
    result = await service_clean.run_once()
    assert result.sessions_deleted == 2
    assert result.total == 2

    async with postgres_session_factory() as session:
        remaining = (
            await session.scalars(select(DashboardSession.user_open_id))
        ).all()
    assert set(remaining) == {"ou_live"}
    assert await service.authenticate(live.session_token)
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(revoked.session_token)
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(expired.session_token)


async def test_cleanup_never_touches_active_sessions_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Active sessions are never cleaned even when very old."""
    service = DashboardAuthService(_settings(), postgres_session_factory)
    created = await service.create_session(_identity("ou_old_active"))
    async with postgres_session_factory() as session:
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(days=365)
        await session.commit()
    result = await CleanupService(
        CleanupStore(postgres_session_factory),
        RetentionPolicy(session_retention_days=1),
        batch_size=10,
    ).run_once()
    assert result.sessions_deleted == 0
    assert await service.authenticate(created.session_token)


async def test_session_user_binding_and_identity_resolution_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session resolves to the same internal User a Feishu identity does,
    so human sessions and Feishu share one authorization surface."""
    service = DashboardAuthService(_settings(), postgres_session_factory)
    created = await service.create_session(_identity("ou_web_user"))
    async with postgres_session_factory() as session:
        user = await session.get(User, created.principal.user_id)
        assert user is not None
        assert user.status == "active"
        row = await session.scalar(select(DashboardSession))
        assert row is not None
        assert row.user_id == created.principal.user_id
        assert hashlib.sha256(created.session_token.encode("utf-8")).hexdigest() == row.token_hash
