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
from lark_ledger.models import Base, EventReplayAudit, ProcessedEvent, ReplyOutbox
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


def _event(event_id: str, *, status: str = "dead") -> ProcessedEvent:
    now = datetime.now(UTC)
    message_id = f"om_sensitive_{event_id}"
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
        last_error_code="TemporaryFailure",
    )


class _Readiness:
    async def check(self, state: Any) -> dict[str, Any]:
        del state
        return {
            "status": "ready",
            "checks": {
                "database": {"status": "ok"},
                "migration": {"status": "ok", "current": "20260808_0014"},
            },
        }


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
    app.state.readiness = _Readiness()
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )
    client.cookies.set(SESSION_COOKIE, created.session_token)
    client.cookies.set(CSRF_COOKIE, created.csrf_token)
    return client, created.csrf_token


async def test_admin_lists_are_bounded_redacted_and_permissioned(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    event = _event("evt-dead")
    reply_id = uuid.uuid4()
    async with factory() as session:
        session.add(event)
        session.add(
            ReplyOutbox(
                id=reply_id,
                event_id=event.event_id,
                message_id="om_full_private_target",
                reply_type="text",
                sequence=0,
                payload_json={"text": "private reply"},
                payload_blob=b"private blob",
                status="dead",
                attempt_count=3,
                last_error_code="HTTPStatusError",
            )
        )
        await session.commit()

    user_client, user_csrf = await _client(factory, "ou_user")
    async with user_client:
        assert (await user_client.get("/api/web/v1/admin/events")).status_code == 403
        assert (
            await user_client.post(
                f"/api/web/v1/admin/outbox/{reply_id}/replay",
                headers={"X-CSRF-Token": user_csrf},
            )
        ).status_code == 403
        assert (
            await user_client.post(
                "/api/web/v1/admin/events/evt-dead/replay",
                headers={"X-CSRF-Token": user_csrf},
                json={"reason": "not an administrator"},
            )
        ).status_code == 403

    admin_client, _ = await _client(factory, "ou_admin")
    async with admin_client:
        events = await admin_client.get("/api/web/v1/admin/events?status=dead")
        assert events.status_code == 200
        event_data = events.json()["items"][0]
        assert event_data["source_message_id"] == "om_se…dead"
        assert {"payload_json", "user_open_id", "lease_owner", "result_summary"}.isdisjoint(
            event_data
        )
        outbox = await admin_client.get("/api/web/v1/admin/outbox?status=dead")
        reply_data = outbox.json()["items"][0]
        assert {"message_id", "payload_json", "payload_blob", "remote_file_key"}.isdisjoint(
            reply_data
        )
        dead = await admin_client.get("/api/web/v1/admin/dead")
        assert dead.json()["event_count"] == 1
        assert dead.json()["reply_count"] == 1
        health = await admin_client.get("/api/web/v1/admin/health")
        assert health.json()["checks"]["migration"]["current"] == "20260808_0014"


async def test_replay_requires_csrf_preflight_and_second_confirmation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    event = _event("evt-replay")
    result_event = _event("evt-result", status="succeeded")
    reply_id = uuid.uuid4()
    async with factory() as session:
        session.add_all([event, result_event])
        session.add(
            ReplyOutbox(
                id=reply_id,
                event_id=result_event.event_id,
                message_id="om_target",
                reply_type="text",
                sequence=0,
                payload_json={"text": "already committed"},
                status="dead",
            )
        )
        await session.commit()

    client, csrf = await _client(factory, "ou_admin")
    headers = {"X-CSRF-Token": csrf}
    async with client:
        no_csrf = await client.post(
            "/api/web/v1/admin/events/evt-replay/replay",
            json={"reason": "temporary failure"},
        )
        assert no_csrf.status_code == 403
        dry_run = await client.post(
            "/api/web/v1/admin/events/evt-replay/replay",
            headers=headers,
            json={"reason": "temporary failure"},
        )
        assert dry_run.json()["preflight"]["eligible"] is True
        mismatch = await client.post(
            "/api/web/v1/admin/events/evt-replay/replay",
            headers=headers,
            json={
                "reason": "temporary failure",
                "execute": True,
                "confirmation_event_id": "wrong-event",
            },
        )
        assert mismatch.status_code == 422
        executed = await client.post(
            "/api/web/v1/admin/events/evt-replay/replay",
            headers=headers,
            json={
                "reason": "temporary failure",
                "execute": True,
                "confirmation_event_id": "evt-replay",
            },
        )
        assert executed.status_code == 200
        assert executed.json()["outcome"] == "requeued"
        result = await client.post(
            f"/api/web/v1/admin/outbox/{reply_id}/replay", headers=headers
        )
        assert result.json()["reset"] == 1

    async with factory() as session:
        replayed = await session.get(ProcessedEvent, "evt-replay")
        reply = await session.get(ReplyOutbox, reply_id)
        audit = (await session.scalars(select(EventReplayAudit))).one()
        assert replayed is not None and replayed.status == "received"
        assert replayed.attempt_count == 0
        assert reply is not None and reply.status == "pending"
        assert audit.operator == "ou_admin"
        assert audit.reason == "temporary failure"
