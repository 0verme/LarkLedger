from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, CategoryBudget, Direction, LedgerEntry
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.web_analytics import WebAnalyticsQueryService
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


def _entry(
    user: str,
    short_id: str,
    amount: str,
    direction: Direction,
    category: str,
    *,
    note: str = "",
    deleted: bool = False,
) -> LedgerEntry:
    occurred = datetime(2026, 8, 8, 4, tzinfo=UTC)
    return LedgerEntry(
        user_open_id=user,
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=direction,
        category=category,
        note=note,
        occurred_at=occurred,
        source_type="text",
        deleted_at=occurred if deleted else None,
    )


async def _client(
    factory: async_sessionmaker[AsyncSession], user: str
) -> tuple[httpx.AsyncClient, str]:
    auth = DashboardAuthService(settings(), factory)
    created = await auth.create_session(
        {"open_id": user, "name": user, "avatar_url": ""}
    )
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


async def test_analytics_and_budget_read_models_are_user_scoped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add_all(
            [
                _entry("ou_a", "ANA01", "100", Direction.INCOME, "工资"),
                _entry("ou_a", "ANA02", "40", Direction.EXPENSE, "餐饮"),
                _entry("ou_b", "ANA01", "999", Direction.EXPENSE, "餐饮"),
                CategoryBudget(user_open_id="ou_a", category="餐饮", amount=Decimal("200")),
            ]
        )
        await session.commit()
        service = WebAnalyticsQueryService(
            session, timezone="Asia/Shanghai", currency="CNY"
        )
        summary, trend, categories, monthly = await service.analytics(
            "ou_a", start_date=date(2026, 8, 1), end_date=date(2026, 8, 8)
        )
        assert summary.income == Decimal("100")
        assert summary.expense == Decimal("40")
        assert summary.entry_count == 2
        assert trend[-1].balance == Decimal("60")
        assert categories[0].category == "餐饮"
        assert categories[0].ratio == Decimal("100")
        assert monthly[0].balance == Decimal("60")
        budgets = await service.budgets(
            "ou_a", now=datetime(2026, 8, 8, 8, tzinfo=UTC)
        )
        assert budgets.total_budget == Decimal("200")
        assert budgets.total_spent == Decimal("40")
        assert budgets.usage_rate == Decimal("20")


async def test_web_report_budget_and_export_reuse_ledger_rules(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add_all(
            [
                _entry("ou_a", "EXP01", "32", Direction.EXPENSE, "餐饮", note="=SUM(A1)"),
                _entry("ou_a", "EXP02", "8", Direction.EXPENSE, "交通", deleted=True),
                _entry("ou_b", "EXP01", "999", Direction.EXPENSE, "隐私"),
                CategoryBudget(
                    user_open_id="ou_b", category="隐私", amount=Decimal("9999")
                ),
            ]
        )
        await session.commit()
    client, csrf = await _client(factory, "ou_a")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        summary = await client.get(
            "/api/web/v1/analytics/summary?period=custom&start_date=2026-08-01&end_date=2026-08-08"
        )
        assert summary.json()["expense"] == "32.00"
        assert (
            await client.get("/api/web/v1/analytics/summary?period=custom")
        ).status_code == 422
        report = await client.get(
            "/api/web/v1/reports?start_date=2026-08-01&end_date=2026-08-08"
        )
        assert report.json()["expense_total"] == "32.00"
        no_csrf = await client.put(
            "/api/web/v1/budgets/%E9%A4%90%E9%A5%AE", json={"amount": "200"}
        )
        assert no_csrf.status_code == 403
        budget = await client.put(
            "/api/web/v1/budgets/%E9%A4%90%E9%A5%AE",
            headers=headers,
            json={"amount": "200"},
        )
        assert budget.json()["total_spent"] == "32.00"
        assert [item["category"] for item in budget.json()["items"]] == ["餐饮"]
        active = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={
                "preset": "custom",
                "start_date": "2026-08-01",
                "end_date": "2026-08-08",
                "include_deleted": False,
            },
        )
        assert active.status_code == 200
        assert active.headers["x-larkledger-row-count"] == "1"
        assert active.content.startswith(b"\xef\xbb\xbf")
        assert b"'=SUM(A1)" in active.content
        assert b"999.00" not in active.content
        with_deleted = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={
                "preset": "custom",
                "start_date": "2026-08-01",
                "end_date": "2026-08-08",
                "include_deleted": True,
            },
        )
        assert with_deleted.headers["x-larkledger-row-count"] == "2"
        invalid_export = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={"preset": "custom"},
        )
        assert invalid_export.status_code == 422
        deleted_budget = await client.delete(
            "/api/web/v1/budgets/%E9%A4%90%E9%A5%AE", headers=headers
        )
        # No budget records remain: the overview reports "no budget" as a null
        # limit rather than zero, so a missing budget is never confused with a
        # ¥0 limit.
        assert deleted_budget.json()["total_budget"] is None
        assert deleted_budget.json()["status"] == "none"


async def test_web_total_budget_period_set_and_delete(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(factory, "ou_a")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        no_csrf = await client.put(
            "/api/web/v1/budgets/total?period=2026-08", json={"amount": "1"}
        )
        assert no_csrf.status_code == 403
        invalid = await client.put(
            "/api/web/v1/budgets/total?period=2026-13",
            headers=headers,
            json={"amount": "1"},
        )
        assert invalid.status_code == 422
        missing = await client.put(
            "/api/web/v1/budgets/total?period=not-a-period",
            headers=headers,
            json={"amount": "1"},
        )
        assert missing.status_code == 422

        created = await client.put(
            "/api/web/v1/budgets/total?period=2026-08",
            headers=headers,
            json={"amount": "12000"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["period"] == "2026-08"
        assert body["total_limit_set"] is True
        assert body["total_budget"] == "12000.00"
        assert body["status"] == "normal"

        # Editing an existing total upserts instead of duplicating.
        updated = await client.put(
            "/api/web/v1/budgets/total?period=2026-08",
            headers=headers,
            json={"amount": "9000"},
        )
        assert updated.json()["total_budget"] == "9000.00"

        listed = await client.get("/api/web/v1/budgets?period=2026-08")
        assert listed.status_code == 200
        assert listed.json()["total_budget"] == "9000.00"

        deleted = await client.delete(
            "/api/web/v1/budgets/total?period=2026-08", headers=headers
        )
        assert deleted.status_code == 200
        assert deleted.json()["total_limit_set"] is False
        assert deleted.json()["total_budget"] is None

