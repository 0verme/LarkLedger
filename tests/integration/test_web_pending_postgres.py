import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.models import Direction, LedgerEntry, PendingCommand, ReplyOutbox
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.pending import PendingPreview, PendingPreviewItem
from lark_ledger.web_api import _auth_service, router

pytestmark = pytest.mark.postgres


async def test_concurrent_web_confirm_executes_frozen_command_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="test-only-secret-that-is-long-enough-123456",
        dashboard_cookie_secure=False,
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
    )
    now = datetime.now(UTC)
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=now,
    )
    preview = PendingPreview(
        code="CA83F2",
        display_code="#C-A83F2",
        entries_total=1,
        expense_count=1,
        expense_total="32.00",
        income_total="0.00",
        currency="CNY",
        risk_reason="图片识别",
        expires_at=(now + timedelta(hours=1)).isoformat(),
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
    )
    async with postgres_session_factory() as session:
        session.add(
            PendingCommand(
                confirmation_code="CA83F2",
                user_open_id="ou_user",
                source_message_id="om_web_confirm",
                transport="feishu",
                source_type="image",
                command_type=command.action.value,
                payload_version=1,
                payload_json=command.model_dump(mode="json"),
                preview_json=preview.as_json(),
                risk_reason="vision",
                status="pending",
                expires_at=now + timedelta(hours=1),
            )
        )
        await session.commit()

    auth = DashboardAuthService(settings, postgres_session_factory)
    created = await auth.create_session(
        {"open_id": "ou_user", "name": "小飞", "avatar_url": ""}
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = postgres_session_factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        client.cookies.set(SESSION_COOKIE, created.session_token)
        client.cookies.set(CSRF_COOKIE, created.csrf_token)
        responses = await asyncio.gather(
            client.post(
                "/api/web/v1/pending/CA83F2/confirm",
                headers={"X-CSRF-Token": created.csrf_token},
            ),
            client.post(
                "/api/web/v1/pending/CA83F2/confirm",
                headers={"X-CSRF-Token": created.csrf_token},
            ),
        )
    assert [response.status_code for response in responses] == [200, 200]
    async with postgres_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(ReplyOutbox)) == 2
        pending = (await session.scalars(select(PendingCommand))).one()
        assert pending.status == "executed"
