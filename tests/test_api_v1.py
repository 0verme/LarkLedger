"""v0.9.0 channel-neutral Client API (/api/v1) contract tests.

The canonical path is ``/api/v1``; the legacy ``/api/client/v1`` prefix must
keep serving identical handlers. Every endpoint resolves identity through the
same bearer-credential service and executes business through the same
``ClientApplicationService``, so both prefixes must behave identically.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.client_schemas import ClientCredentialCreateRequest
from lark_ledger.config import Settings
from lark_ledger.main import create_app
from lark_ledger.models import Base, LedgerEntry
from lark_ledger.services.client_auth import ClientCredentialService
from lark_ledger.services.identity import IdentityService


@pytest_asyncio.fixture
async def client_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _credential(
    factory: async_sessionmaker[AsyncSession],
    *,
    subject: str,
) -> str:
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)
        created = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(name="v1 device"),
        )
        return created.token


def _app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = create_app(Settings(_env_file=None))
    app.state.settings = Settings(_env_file=None)
    app.state.session_factory = factory
    return app


async def _client(factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    app = _app(factory)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    )


async def test_api_v1_me_and_error_envelope(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _credential(client_factory, subject="ou_v1_me")
    async with await _client(client_factory) as client:
        me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["source_channel"] == "client_api"
    assert body["user_id"]

    async with await _client(client_factory) as client:
        anonymous = await client.get("/api/v1/me")
    assert anonymous.status_code == 401
    error = anonymous.json()["error"]
    assert error["code"] == "authentication_required"
    assert error["request_id"]


async def test_api_v1_and_legacy_prefix_behave_identically(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _credential(client_factory, subject="ou_dual")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "amount": "18.00",
        "direction": "expense",
        "category": "餐饮",
        "note": "早餐",
        "occurred_at": "2026-08-14T08:00:00+08:00",
    }
    async with await _client(client_factory) as client:
        canonical = await client.post(
            "/api/v1/transactions",
            headers=headers | {"Idempotency-Key": "v1-canonical"},
            json=payload,
        )
        legacy = await client.post(
            "/api/client/v1/transactions",
            headers=headers | {"Idempotency-Key": "v1-legacy"},
            json=payload,
        )
    assert canonical.status_code == 201
    assert legacy.status_code == 201
    # Same business fact via both prefixes → structurally equivalent results;
    # only the resource id / short_id may differ (two distinct rows).
    canonical_resource = canonical.json()["resource"]
    legacy_resource = legacy.json()["resource"]
    assert canonical_resource is not None and legacy_resource is not None
    # Message text embeds each row's short id; strip the ref before comparing.
    canonical_message = re.sub(
        r"#[A-Z0-9]+ ", "#REF ", canonical.json()["message"]
    )
    legacy_message = re.sub(r"#[A-Z0-9]+ ", "#REF ", legacy.json()["message"])
    assert canonical_message == legacy_message
    for key in canonical_resource:
        if key not in ("id", "short_id"):
            assert canonical_resource[key] == legacy_resource[key], key

    async with await _client(client_factory) as client:
        canonical_get = await client.get(
            f"/api/v1/transactions/{canonical_resource['id']}", headers=headers
        )
        legacy_get = await client.get(
            f"/api/client/v1/entries/{canonical_resource['short_id']}", headers=headers
        )
    assert canonical_get.status_code == 200
    assert legacy_get.status_code == 200
    assert canonical_get.json()["entry"]["id"] == legacy_get.json()["entry"]["id"]


async def test_api_v1_ledger_detail_is_authorization_gated(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _credential(client_factory, subject="ou_ledger_detail")
    headers = {"Authorization": f"Bearer {token}"}
    async with await _client(client_factory) as client:
        ledgers = await client.get("/api/v1/ledgers", headers=headers)
        assert ledgers.status_code == 200
        first = ledgers.json()["items"][0]
        detail = await client.get(f"/api/v1/ledgers/{first['id']}", headers=headers)
        missing = await client.get(
            f"/api/v1/ledgers/{uuid.uuid4()}", headers=headers
        )
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]
    assert missing.status_code == 404


async def test_api_v1_reads_goals_overview_insights_recurring(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _credential(client_factory, subject="ou_reads")
    headers = {"Authorization": f"Bearer {token}"}
    async with await _client(client_factory) as client:
        for path in (
            "/api/v1/goals",
            "/api/v1/overview",
            "/api/v1/insights",
            "/api/v1/recurring-rules",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, f"{path} -> {response.status_code}"


async def test_api_v1_transaction_update_delete_with_idempotency(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _credential(client_factory, subject="ou_mutate")
    headers = {"Authorization": f"Bearer {token}"}
    idem = {"Idempotency-Key": "v1-key-abc"}
    payload = {
        "amount": "28.00",
        "direction": "expense",
        "category": "餐饮",
        "note": "午餐",
        "occurred_at": "2026-08-14T12:00:00+08:00",
    }
    async with await _client(client_factory) as client:
        created = await client.post(
            "/api/v1/transactions", headers=headers | idem, json=payload
        )
        replay = await client.post(
            "/api/v1/transactions", headers=headers | idem, json=payload
        )
    assert created.status_code == 201
    assert replay.status_code == 201
    assert created.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    entry_id = created.json()["resource"]["id"]

    async with await _client(client_factory) as client:
        listed = await client.get("/api/v1/transactions", headers=headers)
        detail = await client.get(f"/api/v1/transactions/{entry_id}", headers=headers)
        updated = await client.patch(
            f"/api/v1/transactions/{entry_id}",
            headers=headers | {"Idempotency-Key": "v1-key-update"},
            json={
                "amount": "30.00",
                "direction": "expense",
                "category": "餐饮",
                "note": "午餐",
                "occurred_at": "2026-08-14T12:00:00+08:00",
                "expected_updated_at": detail.json()["entry"]["updated_at"],
            },
        )
        deleted = await client.request(
            "DELETE",
            f"/api/v1/transactions/{entry_id}",
            headers=headers | {"Idempotency-Key": "v1-key-delete"},
            json={"expected_updated_at": updated.json()["entry"]["updated_at"]},
        )
    assert listed.status_code == 200
    assert listed.json()["items"], "transaction list must contain the created entry"
    assert detail.status_code == 200
    assert updated.status_code == 200
    assert deleted.status_code == 200

    async with client_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(LedgerEntry).where(LedgerEntry.id == uuid.UUID(entry_id))
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].deleted_at is not None


async def test_api_v1_read_scope_rejects_writes(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with client_factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_read_only")
        created = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(
                name="read only", scopes=["ledger:read"]
            ),
        )
        token = created.token
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "amount": "1.00",
        "direction": "expense",
        "category": "餐饮",
        "note": "x",
        "occurred_at": "2026-08-14T08:00:00+08:00",
    }
    async with await _client(client_factory) as client:
        me = await client.get("/api/v1/me", headers=headers)
        denied = await client.post("/api/v1/transactions", headers=headers, json=payload)
    assert me.status_code == 200
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"
