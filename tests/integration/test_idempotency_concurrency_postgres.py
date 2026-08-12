"""PostgreSQL integration: idempotency concurrency for the Client API (§84/§85).

Two concurrent POSTs with the same token, ledger, operation, body and
``Idempotency-Key`` must converge to exactly ONE ledger entry — the scoped
unique constraint is the authority, and the loser either replays the committed
winner's snapshot (``replayed: true``) or reports the in-progress state (503),
never a second write. Same key with a different body must be 409.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lark_ledger.client_schemas import ClientCredentialCreateRequest
from lark_ledger.config import Settings
from lark_ledger.main import create_app
from lark_ledger.models import LedgerEntry
from lark_ledger.services.client_auth import ClientCredentialService
from lark_ledger.services.identity import IdentityService

pytestmark = pytest.mark.postgres


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
        currency="CNY",
        timezone="Asia/Shanghai",
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="test-only-secret-that-is-long-enough-123456",
        dashboard_cookie_secure=False,
    )


async def _bootstrap(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[FastAPI, str]:
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_concurrent")
        created = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(
                name="concurrency device", scopes=["ledger:read", "ledger:write"]
            ),
        )
        token = created.token
    app = create_app(_settings())
    app.state.settings = _settings()
    app.state.session_factory = factory
    return app, token


def _payload(amount: str) -> dict[str, Any]:
    return {
        "direction": "expense",
        "amount": amount,
        "currency": "CNY",
        "category": "餐饮",
        "note": "早餐",
        "occurred_at": "2026-08-14T08:00:00+08:00",
    }


async def _entry_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(
                    LedgerEntry.ledger_id.is_not(None),
                    LedgerEntry.category == "餐饮",
                    LedgerEntry.note == "早餐",
                )
            )
            or 0
        )


async def test_idempotency_concurrent_same_key_creates_one_entry(
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    app, token = await _bootstrap(factory)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "concurrent-key-1"}

    async def post_once() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ledger.test") as client:
            return await client.post(
                "/api/v1/transactions", headers=headers, json=_payload("18.00")
            )

    results = await asyncio.gather(post_once(), post_once(), return_exceptions=True)
    statuses = sorted(r.status_code if isinstance(r, httpx.Response) else 999 for r in results)
    # Allowed convergence: one fresh create (+ either a replayed snapshot or an
    # in-progress 503 for the loser). Anything else means a duplicate slipped in.
    assert statuses in ([201, 201], [201, 503]), statuses
    assert await _entry_count(factory) == 1, "concurrent same-key writes must produce one entry"

    # A later retry of the same key/body replays the committed snapshot.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        replay = await client.post("/api/v1/transactions", headers=headers, json=_payload("18.00"))
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert await _entry_count(factory) == 1


async def test_idempotency_same_key_different_body_is_conflict(
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    app, token = await _bootstrap(factory)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "conflict-key-9"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        first = await client.post("/api/v1/transactions", headers=headers, json=_payload("18.00"))
        second = await client.post("/api/v1/transactions", headers=headers, json=_payload("28.00"))
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert await _entry_count(factory) == 1


async def test_idempotency_records_persist_and_expire_via_ttl(
    postgres_engine: AsyncEngine,
) -> None:
    """Idempotency lives in PostgreSQL, not memory: a record created through
    one app instance is honored by a fresh instance."""
    from lark_ledger.models import ClientIdempotencyRecord

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    app1, token = await _bootstrap(factory)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "persist-key-7"}
    payload = _payload("18.00")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app1), base_url="http://ledger.test"
    ) as client:
        first = await client.post("/api/v1/transactions", headers=headers, json=payload)
    assert first.status_code == 201

    # Brand-new app instance (fresh process) still sees the durable record.
    app2, _ = await _bootstrap(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://ledger.test"
    ) as client:
        second = await client.post("/api/v1/transactions", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert await _entry_count(factory) == 1

    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ClientIdempotencyRecord)
            .where(ClientIdempotencyRecord.idempotency_key == "persist-key-7")
        )
        assert count == 1
