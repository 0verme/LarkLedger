from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    REPLAY_SAFETY_VERSION,
    build_stored_payload,
)
from lark_ledger.models import (
    Base,
    Direction,
    LedgerEntry,
    PendingCommand,
    ProcessedEvent,
    ReplyOutbox,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    OAUTH_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.pending import PendingPreview, PendingPreviewItem
from lark_ledger.services.web_ledger import WebLedgerQueryService
from lark_ledger.services.web_pending import WebPendingQueryService
from lark_ledger.web_api import _auth_service, router


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


def _event(event_id: str, *, status: str = "dead") -> ProcessedEvent:
    now = datetime.now(UTC)
    message_id = f"om_{event_id}"
    event = {
        "sender": {"sender_id": {"open_id": "ou_private"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": '{"text": "private financial text"}',
        },
    }
    return ProcessedEvent(
        event_id=event_id,
        payload_json=build_stored_payload(
            event_id, event, transport="webhook", received_at=now - timedelta(minutes=2)
        ),
        payload_version=PAYLOAD_VERSION,
        replay_safety_version=REPLAY_SAFETY_VERSION,
        transport="webhook",
        status=status,
        attempt_count=3,
        source_message_id=message_id,
        user_open_id="ou_private",
        received_at=now - timedelta(minutes=2),
        last_error_code="TemporaryFailure",
    )


def _preview(code: str, expires_at: datetime) -> dict[str, Any]:
    return PendingPreview(
        code=code,
        display_code=f"#C-{code[1:]}",
        entries_total=1,
        expense_count=1,
        expense_total="32.00",
        income_total="0.00",
        currency="CNY",
        risk_reason="????",
        expires_at=expires_at.isoformat(),
        items=[
            PendingPreviewItem(
                index=None,
                direction="expense",
                amount="32.00",
                currency="CNY",
                category="??",
                occurred_at="2026-08-08 12:00",
                note="??",
            )
        ],
    ).as_json()


_MISSING = object()


def _pending(
    code: str,
    *,
    user: str = "ou_user",
    status: str = "pending",
    expires_at: datetime,
    source_message_id: str | None | object = _MISSING,
) -> PendingCommand:
    if source_message_id is _MISSING:
        source_message_id = f"om_{code}"
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("32"),
        direction=Direction.EXPENSE,
        category="??",
        note="??",
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
    )
    return PendingCommand(
        confirmation_code=code,
        user_open_id=user,
        source_message_id=source_message_id,
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


async def _client(
    factory: async_sessionmaker[AsyncSession],
    user: str,
    *,
    service: DashboardAuthService | None = None,
    processor: Any = None,
    reply_worker: Any = None,
) -> tuple[httpx.AsyncClient, str]:
    auth = service or DashboardAuthService(settings(), factory)
    created = await auth.create_session(
        {"open_id": user, "name": user, "avatar_url": ""}
    )
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    if processor is not None:
        app.state.processor = processor
    if reply_worker is not None:
        app.state.reply_worker = reply_worker
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created.csrf_token


async def test_oauth_callback_success_sets_session_and_csrf_cookies(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(settings(), factory)
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    identity = {
        "open_id": "ou_new",
        "name": "新用户",
        "avatar_url": "",
    }
    with patch.object(
        DashboardAuthService,
        "exchange_identity",
        new=AsyncMock(return_value=identity),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://ledger.test",
            follow_redirects=False,
        ) as client:
            login = await client.get("/api/web/v1/auth/login?next=/entries")
            assert login.status_code == 302
            state = httpx.URL(login.headers["location"]).params["state"]
            oauth_cookie = login.cookies.get(OAUTH_COOKIE)
            assert oauth_cookie
            client.cookies.set(OAUTH_COOKIE, oauth_cookie)

            callback = await client.get(
                "/api/web/v1/auth/callback",
                params={"code": "one-time-code", "state": state},
            )
            assert callback.status_code == 303
            assert callback.headers["location"] == "/entries"
            set_cookies = "; ".join(callback.headers.get_list("set-cookie"))
            assert f"{SESSION_COOKIE}=" in set_cookies
            assert f"{CSRF_COOKIE}=" in set_cookies

            session_cookie = callback.cookies.get(SESSION_COOKIE)
            csrf_cookie = callback.cookies.get(CSRF_COOKIE)
            assert session_cookie and csrf_cookie
            client.cookies.set(SESSION_COOKIE, session_cookie)
            client.cookies.set(CSRF_COOKIE, csrf_cookie)
            me = await client.get("/api/web/v1/me")
            assert me.status_code == 200
            assert me.json()["open_id"] == "ou_new"


async def test_web_session_selects_owned_ledger_and_rejects_foreign_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_client, csrf = await _client(factory, "ou_owner")
    outsider_client, outsider_csrf = await _client(factory, "ou_outsider")
    async with owner_client, outsider_client:
        created = await owner_client.post(
            "/api/web/v1/ledgers",
            headers={"X-CSRF-Token": csrf},
            json={"name": "旅行"},
        )
        assert created.status_code == 201
        ledger_id = created.json()["id"]
        selected = await owner_client.post(
            f"/api/web/v1/ledgers/{ledger_id}/select",
            headers={"X-CSRF-Token": csrf},
        )
        assert selected.status_code == 200
        assert selected.json()["is_current"] is True
        current = await owner_client.get("/api/web/v1/ledgers/current")
        assert current.json()["name"] == "旅行"

        rejected = await outsider_client.post(
            f"/api/web/v1/ledgers/{ledger_id}/select",
            headers={"X-CSRF-Token": outsider_csrf},
        )
        assert rejected.status_code == 404


async def test_oauth_callback_error_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = DashboardAuthService(settings(), factory)
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ledger.test",
        follow_redirects=False,
    ) as client:
        login = await client.get("/api/web/v1/auth/login?next=/entries")
        state = httpx.URL(login.headers["location"]).params["state"]
        oauth_cookie = login.cookies.get(OAUTH_COOKIE)
        client.cookies.set(OAUTH_COOKIE, oauth_cookie)

        cancelled = await client.get(
            "/api/web/v1/auth/callback",
            params={"code": "one-time-code", "state": state, "error": "access_denied"},
        )
        assert cancelled.status_code == 401

        tampered = await client.get(
            "/api/web/v1/auth/callback",
            params={"code": "one-time-code", "state": "wrong-state"},
        )
        assert tampered.status_code == 401


async def test_dashboard_entries_validation_and_invalid_ref_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add(_entry("ou_user", "ABC12", "32", Direction.EXPENSE, "??"))
        await session.commit()
    client, csrf = await _client(factory, "ou_user")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        dash = await client.get("/api/web/v1/dashboard")
        assert dash.status_code == 200
        assert "month_balance" in dash.json()

        bad_range = await client.get(
            "/api/web/v1/entries",
            params={"start": "2026-08-08T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
        )
        assert bad_range.status_code == 422
        bad_amounts = await client.get(
            "/api/web/v1/entries", params={"amount_min": "100", "amount_max": "50"}
        )
        assert bad_amounts.status_code == 422

        invalid_detail = await client.get("/api/web/v1/entries/INVALID")
        assert invalid_detail.status_code == 404
        invalid_patch = await client.patch(
            "/api/web/v1/entries/INVALID",
            headers=headers,
            json={"expected_updated_at": "2026-08-08T00:00:00Z", "amount": "35"},
        )
        assert invalid_patch.status_code == 404


async def test_mutate_entry_refresh_missing_404(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add(_entry("ou_user", "ABC12", "32", Direction.EXPENSE, "??"))
        await session.commit()
    client, csrf = await _client(factory, "ou_user")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        detail = await client.get("/api/web/v1/entries/ABC12")
        version = detail.json()["entry"]["updated_at"]
        real_method = WebLedgerQueryService.entry_detail
        calls = {"n": 0}

        async def fake_entry_detail(
            self: WebLedgerQueryService, scope: Any, short_id: str
        ) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_method(self, scope, short_id)
            return None

        with patch.object(WebLedgerQueryService, "entry_detail", fake_entry_detail):
            resp = await client.patch(
                "/api/web/v1/entries/ABC12",
                headers=headers,
                json={"expected_updated_at": version, "amount": "35"},
            )
            assert resp.status_code == 404


class _ProcessorStub:
    exchange_rates: Any = None

    def __init__(self) -> None:
        self.signalled: list[Any] = []

    @property
    def _pending_store(self) -> None:
        return None

    async def _signal_or_deliver(self, outbox: Any) -> None:
        self.signalled.append(outbox)


async def test_pending_http_query_and_action_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                _pending("CA83F2", expires_at=now + timedelta(hours=1)),
                _pending(
                    "CB83F2",
                    expires_at=now + timedelta(hours=1),
                    source_message_id=None,
                ),
            ]
        )
        await session.commit()
    client, csrf = await _client(factory, "ou_user")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        listed = await client.get("/api/web/v1/pending")
        assert listed.status_code == 200
        assert listed.json()["total"] == 2

        detail = await client.get("/api/web/v1/pending/CA83F2")
        assert detail.status_code == 200
        assert detail.json()["pending"]["confirmation_id"] == "#C-A83F2"

        invalid = await client.get("/api/web/v1/pending/NO!")
        assert invalid.status_code == 404
        invalid_confirm = await client.post(
            "/api/web/v1/pending/NO!/confirm", headers=headers
        )
        assert invalid_confirm.status_code == 404
        no_source = await client.post(
            "/api/web/v1/pending/CB83F2/confirm", headers=headers
        )
        assert no_source.status_code == 409


async def test_pending_action_signal_and_detail_missing_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                _pending("CA83F2", expires_at=now + timedelta(hours=1)),
                _pending("CB83F2", expires_at=now + timedelta(hours=1)),
            ]
        )
        await session.commit()
    processor = _ProcessorStub()
    client, csrf = await _client(factory, "ou_user", processor=processor)
    headers = {"X-CSRF-Token": csrf}
    async with client:
        confirmed = await client.post(
            "/api/web/v1/pending/CA83F2/confirm", headers=headers
        )
        assert confirmed.status_code == 200
        assert len(processor.signalled) == 1

        with patch.object(
            WebPendingQueryService, "detail", AsyncMock(return_value=None)
        ):
            resp = await client.post(
                "/api/web/v1/pending/CB83F2/confirm", headers=headers
            )
            assert resp.status_code == 404


class _WorkerStub:
    def __init__(self) -> None:
        self.woken = 0

    def wakeup(self) -> None:
        self.woken += 1


async def test_replay_outbox_error_branches_and_worker_wakeup(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    dead_id = uuid.uuid4()
    sent_id = uuid.uuid4()
    async with factory() as session:
        session.add_all(
            [
                ReplyOutbox(
                    id=dead_id,
                    event_id="evt-dead",
                    message_id="om_dead",
                    reply_type="text",
                    sequence=0,
                    payload_json={"text": "hello"},
                    status="dead",
                ),
                ReplyOutbox(
                    id=sent_id,
                    event_id="evt-sent",
                    message_id="om_sent",
                    reply_type="text",
                    sequence=0,
                    payload_json={"text": "hello"},
                    status="sent",
                ),
            ]
        )
        await session.commit()
    worker = _WorkerStub()
    client, csrf = await _client(factory, "ou_admin", reply_worker=worker)
    headers = {"X-CSRF-Token": csrf}
    async with client:
        missing = await client.post(
            f"/api/web/v1/admin/outbox/{uuid.uuid4()}/replay", headers=headers
        )
        assert missing.status_code == 404
        not_replayable = await client.post(
            f"/api/web/v1/admin/outbox/{sent_id}/replay", headers=headers
        )
        assert not_replayable.status_code == 409
        replayed = await client.post(
            f"/api/web/v1/admin/outbox/{dead_id}/replay", headers=headers
        )
        assert replayed.status_code == 200
        assert replayed.json()["reset"] == 1
        assert worker.woken == 1


async def test_replay_event_error_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add(_event("evt-succeeded", status="succeeded"))
        await session.commit()
    client, csrf = await _client(factory, "ou_admin")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        too_long = "e" * 129
        invalid = await client.post(
            f"/api/web/v1/admin/events/{too_long}/replay",
            headers=headers,
            json={"reason": "????"},
        )
        assert invalid.status_code == 422
        rejected = await client.post(
            "/api/web/v1/admin/events/evt-succeeded/replay",
            headers=headers,
            json={
                "reason": "????",
                "execute": True,
                "confirmation_event_id": "evt-succeeded",
            },
        )
        assert rejected.status_code == 409


async def test_admin_health_503_when_readiness_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = await _client(factory, "ou_admin")
    async with client:
        resp = await client.get("/api/web/v1/admin/health")
        assert resp.status_code == 503


async def test_analytics_period_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add_all(
            [
                _entry("ou_user", "ANA01", "100", Direction.INCOME, "??"),
                _entry("ou_user", "ANA02", "40", Direction.EXPENSE, "??"),
            ]
        )
        await session.commit()
    client, _ = await _client(factory, "ou_user")
    async with client:
        overview = await client.get("/api/web/v1/analytics?period=year")
        assert overview.status_code == 200
        assert "summary" in overview.json()
        trend = await client.get("/api/web/v1/analytics/trend?period=30d")
        assert trend.status_code == 200
        categories = await client.get("/api/web/v1/analytics/categories?period=7d")
        assert categories.status_code == 200
        monthly = await client.get("/api/web/v1/analytics/monthly?period=year")
        assert monthly.status_code == 200
        invalid_range = await client.get(
            "/api/web/v1/analytics/summary",
            params={
                "period": "custom",
                "start_date": "2026-09-01",
                "end_date": "2026-08-01",
            },
        )
        assert invalid_range.status_code == 422


async def test_budgets_get_and_report_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add(_entry("ou_user", "ANA02", "40", Direction.EXPENSE, "??"))
        await session.commit()
    client, _ = await _client(factory, "ou_user")
    async with client:
        budgets = await client.get("/api/web/v1/budgets")
        assert budgets.status_code == 200
        invalid_report = await client.get(
            "/api/web/v1/reports",
            params={"start_date": "2026-09-01", "end_date": "2026-08-01"},
        )
        assert invalid_report.status_code == 422
        empty_report = await client.get(
            "/api/web/v1/reports",
            params={"start_date": "2020-01-01", "end_date": "2020-01-31"},
        )
        assert empty_report.status_code == 404


async def test_export_preset_branches(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add(_entry("ou_user", "EXP01", "32", Direction.EXPENSE, "??"))
        await session.commit()
    client, csrf = await _client(factory, "ou_user")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        this_month = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={"preset": "this_month", "include_deleted": False},
        )
        assert this_month.status_code == 200
        last_90 = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={"preset": "last_90_days", "include_deleted": False},
        )
        assert last_90.status_code == 200
        all_export = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={"preset": "all", "include_deleted": False},
        )
        assert all_export.status_code == 200


async def test_export_empty_range_422(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf = await _client(factory, "ou_user")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        empty = await client.post(
            "/api/web/v1/exports",
            headers=headers,
            json={"preset": "all", "include_deleted": False},
        )
        assert empty.status_code == 422
