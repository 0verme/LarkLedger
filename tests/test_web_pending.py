from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction, LedgerEntry, PendingCommand, ReplyOutbox
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.pending import PendingPreview, PendingPreviewItem
from lark_ledger.services.web_pending import WebPendingQueryService
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


def _preview(code: str, expires_at: datetime) -> dict[str, Any]:
    return PendingPreview(
        code=code,
        display_code=f"#C-{code[1:]}",
        entries_total=1,
        expense_count=1,
        expense_total="32.00",
        income_total="0.00",
        currency="CNY",
        risk_reason="图片识别",
        expires_at=expires_at.isoformat(),
        items=[
            PendingPreviewItem(
                index=None,
                direction="expense",
                amount="32.00",
                currency="CNY",
                category="餐饮",
                occurred_at="2026-08-08 12:00",
                note="午饭",
            )
        ],
    ).as_json()


def _pending(
    code: str,
    *,
    user: str = "ou_user",
    status: str = "pending",
    expires_at: datetime,
) -> PendingCommand:
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
    )
    return PendingCommand(
        confirmation_code=code,
        user_open_id=user,
        source_message_id=f"om_{code}",
        transport="feishu",
        source_type="image",
        command_type=command.action.value,
        payload_version=1,
        payload_json=command.model_dump(mode="json"),
        preview_json=_preview(code, expires_at),
        risk_reason="vision",
        status=status,
        expires_at=expires_at,
    )


async def test_pending_read_model_groups_expired_and_scopes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                _pending("CA83F2", expires_at=now + timedelta(hours=1)),
                _pending("CB83F2", expires_at=now - timedelta(minutes=1)),
                _pending(
                    "CC83F2", status="executed", expires_at=now + timedelta(hours=1)
                ),
                _pending("CD83F2", user="ou_other", expires_at=now + timedelta(hours=1)),
            ]
        )
        await session.commit()
        query = WebPendingQueryService(session)
        active = await query.list_pending(
            "ou_user", group="pending", page=1, page_size=20, now=now
        )
        assert [item.confirmation_id for item in active.items] == ["#C-A83F2"]
        closed = await query.list_pending(
            "ou_user", group="closed", page=1, page_size=20, now=now
        )
        assert closed.items[0].status == "expired"
        assert await query.detail("ou_other", "CA83F2", now=now) is None
        detail = await query.detail("ou_user", "#C-A83F2", now=now)
        assert detail is not None
        assert detail.preview["items"][0]["note"] == "午饭"
        assert "payload_json" not in detail.model_dump()


async def test_pending_http_confirm_cancel_csrf_and_cross_user(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                _pending("CA83F2", expires_at=now + timedelta(hours=1)),
                _pending("CB83F2", expires_at=now + timedelta(hours=1)),
                _pending("CC83F2", user="ou_other", expires_at=now + timedelta(hours=1)),
                _pending("CD83F2", expires_at=now - timedelta(minutes=1)),
            ]
        )
        await session.commit()
    auth = DashboardAuthService(settings(), factory)
    created = await auth.create_session(
        {"open_id": "ou_user", "name": "小飞", "avatar_url": ""}
    )
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, created.session_token)
        client.cookies.set(CSRF_COOKIE, created.csrf_token)
        assert (await client.get("/api/web/v1/pending/CC83F2")).status_code == 404
        assert (await client.post("/api/web/v1/pending/CA83F2/confirm")).status_code == 403
        headers = {"X-CSRF-Token": created.csrf_token}
        confirmed = await client.post(
            "/api/web/v1/pending/CA83F2/confirm", headers=headers
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["pending"]["pending"]["status"] == "executed"
        repeated = await client.post(
            "/api/web/v1/pending/CA83F2/confirm", headers=headers
        )
        assert repeated.status_code == 200
        cancelled = await client.post(
            "/api/web/v1/pending/CB83F2/cancel", headers=headers
        )
        assert cancelled.status_code == 200
        expired = await client.post(
            "/api/web/v1/pending/CD83F2/confirm", headers=headers
        )
        assert expired.status_code == 409
        cross_user = await client.post(
            "/api/web/v1/pending/CC83F2/confirm", headers=headers
        )
        assert cross_user.status_code == 404

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(ReplyOutbox)) == 4
