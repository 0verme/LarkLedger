"""Web API tests for the dead-letter operations console (P44).

Covers authorization (unauthenticated / normal user / admin), list / detail
filters, replay / resolve actions, idempotent double-submit behavior, audit
correlation, and the no-secrets redaction contract.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    REPLAY_SAFETY_VERSION,
    build_stored_payload,
)
from lark_ledger.models import Base, DeadLetterAction, ProcessedEvent, ReplyOutbox
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
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


def _secret_event(event_id: str, *, status: str = "dead") -> ProcessedEvent:
    """An event whose payload / summary deliberately contains secret material."""
    now = datetime.now(UTC)
    message_id = "om_secret_target_1234567890"
    event = {
        "sender": {"sender_id": {"open_id": "ou_private"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": "private financial text"}),
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
        last_error_code="HTTPStatusError",
        result_summary=(
            "HTTPStatusError: Client error '400 Bad Request' for url "
            "'https://open.feishu.cn/open-apis/im/v1/messages/om_accept/reply'"
        ),
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

    class _NoopWorker:
        def wakeup(self) -> None:
            return None

    app.state.reply_worker = _NoopWorker()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created.csrf_token


async def _seed_outbox(factory: async_sessionmaker[AsyncSession]) -> str:
    outbox_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(
            ReplyOutbox(
                id=outbox_id,
                event_id=None,
                message_id="om_accept",
                reply_type="text",
                sequence=0,
                transport="feishu",
                payload_json={"text": "已记录支出 ¥100"},
                status="dead",
                attempt_count=1,
                last_error_code="HTTPStatusError",
                result_summary=(
                    "HTTPStatusError: Client error '400 Bad Request' for url "
                    "'https://open.feishu.cn/open-apis/im/v1/messages/om_accept/reply'"
                ),
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=1),
            )
        )
        await session.commit()
    return str(outbox_id)


_SECRET_WORDS = (
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "database url",
    "postgres",
    "raw payload",
    "financial",
)


def _assert_no_secrets(payload: Any, where: str) -> None:
    blob = json.dumps(payload, ensure_ascii=False).lower()
    for word in _SECRET_WORDS:
        assert word not in blob, f"{word!r} leaked in {where}"


async def test_unauthenticated_and_normal_user_forbidden(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_outbox(factory)
    admin_client, _ = await _client(factory, "ou_admin")
    # unauthenticated (no session cookie) → 401/403
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ledger.test")
    async with anon:
        assert (await anon.get("/api/web/v1/admin/dead-letters")).status_code == 401
        assert (
            await anon.post(
                f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/resolve",
                json={"reason": "anonymous action"},
            )
        ).status_code == 403  # csrf_principal rejects unauthenticated POSTs
    await anon.aclose()

    user_client, user_csrf = await _client(factory, "ou_user")
    async with user_client:
        assert (
            await user_client.get("/api/web/v1/admin/dead-letters")
        ).status_code == 403
        assert (
            await user_client.get(f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}")
        ).status_code == 403
        assert (
            await user_client.post(
                f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/replay",
                headers={"X-CSRF-Token": user_csrf},
                json={"reason": "normal user cannot replay"},
            )
        ).status_code == 403
        assert (
            await user_client.post(
                f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/resolve",
                headers={"X-CSRF-Token": user_csrf},
                json={"reason": "normal user cannot resolve"},
            )
        ).status_code == 403
    await user_client.aclose()
    await admin_client.aclose()


async def test_admin_list_detail_and_filters(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_outbox(factory)
    async with factory() as session:
        session.add(_secret_event("evt-historical-1"))
        await session.commit()

    admin_client, _ = await _client(factory, "ou_admin")
    async with admin_client:
        response = await admin_client.get("/api/web/v1/admin/dead-letters")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        sources = {item["source"] for item in data["items"]}
        assert sources == {"outbox", "events"}
        _assert_no_secrets(data, "list")

        # source filter
        events_only = await admin_client.get(
            "/api/web/v1/admin/dead-letters?source=events"
        )
        assert events_only.json()["total"] == 1
        assert events_only.json()["items"][0]["source"] == "events"

        # reason filter
        rejected = await admin_client.get(
            "/api/web/v1/admin/dead-letters?reason=remote_rejected"
        )
        assert rejected.json()["total"] == 2
        assert all(
            item["reason_category"] == "remote_rejected"
            for item in rejected.json()["items"]
        )

        # retryable filter
        retryable = await admin_client.get(
            "/api/web/v1/admin/dead-letters?retryable=true"
        )
        assert retryable.json()["total"] == 0

        # pagination
        paged = await admin_client.get(
            "/api/web/v1/admin/dead-letters?page=1&page_size=1&sort=dead_at"
        )
        assert paged.json()["pages"] == 2
        assert len(paged.json()["items"]) == 1

        # detail
        detail = await admin_client.get(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}"
        )
        assert detail.status_code == 200
        detail_data = detail.json()
        assert detail_data["reason_category"] == "remote_rejected"
        assert detail_data["terminal"] is True
        assert detail_data["replay_safe"] is False
        _assert_no_secrets(detail_data, "detail")
        # message ids masked
        assert detail_data["message_id"] != "om_accept"

        # detail not found
        assert (
            await admin_client.get(
                f"/api/web/v1/admin/dead-letters/outbox/{uuid.uuid4()}"
            )
        ).status_code == 404

        # event detail exposes no payload / user id
        event_detail = await admin_client.get(
            "/api/web/v1/admin/dead-letters/events/evt-historical-1"
        )
        assert event_detail.status_code == 200
        _assert_no_secrets(event_detail.json(), "event detail")
        assert "user_open_id" not in event_detail.json()
        assert "payload_json" not in event_detail.json()
    await admin_client.aclose()


async def test_admin_replay_flow(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_outbox(factory)
    admin_client, admin_csrf = await _client(factory, "ou_admin")
    async with admin_client:
        # replay with CSRF
        response = await admin_client.post(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/replay",
            headers={"X-CSRF-Token": admin_csrf},
            json={"reason": "transient retry after maintenance"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["outcome"] == "requeued"
        assert result["before_status"] == "dead"
        assert result["after_status"] == "pending"

        # duplicate replay → 409 conflict
        duplicate = await admin_client.post(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/replay",
            headers={"X-CSRF-Token": admin_csrf},
            json={"reason": "double click"},
        )
        assert duplicate.status_code == 409

        # audit row recorded with request correlation
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(DeadLetterAction).where(
                            DeadLetterAction.target_id == outbox_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].action == "replay"
        assert rows[0].operator == "ou_admin"

        # short reason rejected
        short = await admin_client.post(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/resolve",
            headers={"X-CSRF-Token": admin_csrf},
            json={"reason": "ab"},
        )
        assert short.status_code == 422
    await admin_client.aclose()


async def test_admin_resolve_idempotent_and_audited(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_outbox(factory)
    admin_client, admin_csrf = await _client(factory, "ou_admin")
    async with admin_client:
        first = await admin_client.post(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/resolve",
            headers={"X-CSRF-Token": admin_csrf},
            json={"reason": "historical test fixture, no replay value"},
        )
        assert first.status_code == 200
        assert first.json()["outcome"] == "resolved"

        second = await admin_client.post(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/resolve",
            headers={"X-CSRF-Token": admin_csrf},
            json={"reason": "duplicate resolve"},
        )
        assert second.status_code == 200
        assert second.json()["outcome"] == "already_resolved"

        # detail shows resolved marker + audit history
        detail = await admin_client.get(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}"
        )
        assert detail.json()["resolved"] is True
        assert len(detail.json()["audit"]) == 1
        _assert_no_secrets(detail.json(), "resolved detail")
    await admin_client.aclose()


async def test_admin_replay_without_csrf_rejected(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_outbox(factory)
    admin_client, _ = await _client(factory, "ou_admin")
    async with admin_client:
        # no CSRF token header → 403
        response = await admin_client.post(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}/replay",
            json={"reason": "missing csrf"},
        )
        assert response.status_code == 403
    await admin_client.aclose()


async def test_security_redaction_across_endpoints(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deliberately poisoned error summaries must never leak secrets anywhere."""
    now = datetime.now(UTC)
    outbox_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            ReplyOutbox(
                id=outbox_id,
                event_id=None,
                message_id="om_accept",
                reply_type="text",
                sequence=0,
                transport="feishu",
                payload_json={"text": "绝密财务文本 ¥99999999"},
                status="dead",
                attempt_count=1,
                last_error_code="HTTPStatusError",
                result_summary=(
                    "HTTPStatusError: Client error '400 Bad Request' for url "
                    "'https://user:supersecretpw@open.feishu.cn/api' "
                    "Authorization: Bearer sk-live-abcdef123456 "
                    "cookie=session=abc123 password=hunter2"
                ),
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
        )
        await session.commit()

    admin_client, _ = await _client(factory, "ou_admin")
    async with admin_client:
        page = await admin_client.get("/api/web/v1/admin/dead-letters?source=outbox")
        _assert_no_secrets(page.json(), "list with poisoned summary")
        summary = page.json()["items"][0]["last_error_summary"]
        # the stored summary itself was already redacted at write time by the
        # worker; assert no credential material survives the API either
        assert "supersecretpw" not in (summary or "")
        assert "sk-live-" not in (summary or "")
        assert "hunter2" not in (summary or "")

        detail = await admin_client.get(
            f"/api/web/v1/admin/dead-letters/outbox/{outbox_id}"
        )
        _assert_no_secrets(detail.json(), "detail with poisoned summary")
        detail_summary = detail.json()["last_error_summary"]
        assert "supersecretpw" not in (detail_summary or "")
        assert "sk-live-" not in (detail_summary or "")
        assert "hunter2" not in (detail_summary or "")
    await admin_client.aclose()
