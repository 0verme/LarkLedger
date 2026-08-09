from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.client_api import router
from lark_ledger.client_schemas import ClientCredentialCreateRequest
from lark_ledger.config import Settings
from lark_ledger.main import create_app
from lark_ledger.models import Base, ClientCredential, ClientIdempotencyRecord, LedgerEntry
from lark_ledger.services.client_auth import (
    ClientAuthenticationError,
    ClientCredentialService,
)
from lark_ledger.services.household_management import HouseholdManagementService
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
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)
        created = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(
                name="test device", expires_at=expires_at
            ),
        )
        return created.token, created.id


def _app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.state.session_factory = factory
    app.include_router(router)
    return app


async def test_bearer_digest_scopes_revocation_and_expiry(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, credential_id = await _credential(client_factory, subject="ou_client")
    service = ClientCredentialService(
        client_factory, currency="CNY", timezone="Asia/Shanghai"
    )
    principal = await service.authenticate(token)
    assert principal.context.source_channel == "client_api"
    assert principal.scopes == frozenset({"ledger:read", "ledger:write"})
    async with client_factory() as session:
        row = await session.get(ClientCredential, uuid.UUID(credential_id))
        assert row is not None
        assert row.token_digest not in token
        assert row.token_prefix == token[:12]
        await ClientCredentialService.revoke(
            session,
            user_id=principal.context.actor_user_id,
            credential_id=row.id,
        )
    with pytest.raises(ClientAuthenticationError):
        await service.authenticate(token)

    expired, _ = await _credential(
        client_factory,
        subject="ou_expired",
        expires_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    async with client_factory() as session:
        row = await session.scalar(
            select(ClientCredential).where(ClientCredential.token_prefix == expired[:12])
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(ClientAuthenticationError, match="expired"):
        await service.authenticate(expired)


async def test_client_api_idempotency_conflict_and_actor_isolation(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_token, _ = await _credential(client_factory, subject="ou_first")
    second_token, _ = await _credential(client_factory, subject="ou_second")
    payload = {
        "amount": "12.50",
        "direction": "expense",
        "category": "food",
        "note": "lunch",
        "occurred_at": "2026-08-09T12:00:00+08:00",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        first_headers = {
            "Authorization": f"Bearer {first_token}",
            "Idempotency-Key": "same-device-key",
        }
        created = await client.post("/api/client/v1/entries", headers=first_headers, json=payload)
        assert created.status_code == 201
        replay = await client.post("/api/client/v1/entries", headers=first_headers, json=payload)
        assert replay.status_code == 201
        assert replay.json()["resource"] == created.json()["resource"]
        assert replay.json()["replayed"] is True

        conflict_payload = payload | {"amount": "13.00"}
        conflict = await client.post(
            "/api/client/v1/entries", headers=first_headers, json=conflict_payload
        )
        assert conflict.status_code == 409

        ledger = await client.post(
            "/api/client/v1/ledgers",
            headers=first_headers | {"Idempotency-Key": "create-second-ledger"},
            json={"name": "second"},
        )
        assert ledger.status_code == 201
        selected = await client.post(
            f"/api/client/v1/ledgers/{ledger.json()['id']}/select",
            headers=first_headers | {"Idempotency-Key": "select-second-ledger"},
        )
        assert selected.status_code == 200
        other_ledger = await client.post(
            "/api/client/v1/entries", headers=first_headers, json=payload
        )
        assert other_ledger.status_code == 201
        assert other_ledger.json()["resource"] != created.json()["resource"]

        second = await client.post(
            "/api/client/v1/entries",
            headers={
                "Authorization": f"Bearer {second_token}",
                "Idempotency-Key": "same-device-key",
            },
            json=payload,
        )
        assert second.status_code == 201
        assert second.json()["resource"] != created.json()["resource"]

    async with client_factory() as session:
        assert len((await session.scalars(select(LedgerEntry))).all()) == 3
        records = (
            await session.scalars(
                select(ClientIdempotencyRecord).where(
                    ClientIdempotencyRecord.operation == "entry.create"
                )
            )
        ).all()
        assert len(records) == 3
        assert len({row.actor_user_id for row in records}) == 2
        assert len({row.ledger_id for row in records}) == 3


async def test_client_api_rejects_cookie_style_or_missing_bearer(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        client.cookies.set("lark_ledger_session", "not-a-bearer")
        response = await client.get("/api/client/v1/me")
    assert response.status_code == 401


async def test_client_api_uses_stable_error_envelope_and_openapi(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None)
    app = create_app(settings)
    app.state.settings = settings
    app.state.session_factory = client_factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        response = await client.get("/api/client/v1/me")
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "valid bearer credential required",
        }
    }
    schema = app.openapi()
    assert "/api/client/v1/entries" in schema["paths"]
    assert "ClientErrorResponse" in schema["components"]["schemas"]


async def test_bearer_rechecks_household_membership_and_falls_back(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with client_factory() as session:
        identity = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
        owner = await identity.resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_household_owner"
        )
        member = await identity.resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_household_member"
        )
        manager = HouseholdManagementService(
            session, currency="CNY", timezone="Asia/Shanghai"
        )
        household = await manager.create(owner.actor_user_id, "family")
        invitation = await manager.invite(
            owner.actor_user_id, household.household.id, member.actor_user_id
        )
        await manager.accept(member.actor_user_id, invitation.id)
        created = await ClientCredentialService.create(
            session,
            user_id=member.actor_user_id,
            current_ledger_id=member.ledger_id,
            request=ClientCredentialCreateRequest(name="member device"),
        )
    service = ClientCredentialService(
        client_factory, currency="CNY", timezone="Asia/Shanghai"
    )
    principal = await service.authenticate(created.token)
    async with client_factory() as session:
        await ClientCredentialService.select_ledger(
            session,
            user_id=member.actor_user_id,
            credential_id=principal.credential_id,
            ledger_id=household.ledger.id,
        )
        await session.commit()
    assert (await service.authenticate(created.token)).context.ledger_id == household.ledger.id

    async with client_factory() as session:
        await HouseholdManagementService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).remove_member(owner.actor_user_id, household.household.id, member.actor_user_id)
        await session.commit()
    fallback = await service.authenticate(created.token)
    assert fallback.context.ledger_id == member.ledger_id


async def test_account_api_and_explicit_entry_account_are_ledger_scoped(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _credential(client_factory, subject="ou_account_api")
    other_token, _ = await _credential(client_factory, subject="ou_account_api_other")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        defaults = await client.get("/api/client/v1/accounts", headers=headers)
        assert defaults.status_code == 200
        assert len(defaults.json()["items"]) == 1
        assert defaults.json()["items"][0]["is_default"] is True

        created = await client.post(
            "/api/client/v1/accounts",
            headers=headers | {"Idempotency-Key": "create-wallet"},
            json={
                "name": "支付宝",
                "type": "asset",
                "subtype": "wallet",
                "provider": "alipay",
                "opening_balance": "100.00",
            },
        )
        assert created.status_code == 201
        account_id = created.json()["id"]
        entry = await client.post(
            "/api/client/v1/entries",
            headers=headers | {"Idempotency-Key": "wallet-entry"},
            json={
                "amount": "20.00",
                "direction": "expense",
                "category": "餐饮",
                "occurred_at": "2026-08-09T12:00:00+08:00",
                "account_id": account_id,
            },
        )
        assert entry.status_code == 201
        assert entry.json()["resource"]["account_id"] == account_id

        denied = await client.post(
            "/api/client/v1/entries",
            headers={
                "Authorization": f"Bearer {other_token}",
                "Idempotency-Key": "foreign-account",
            },
            json={
                "amount": "20.00",
                "direction": "expense",
                "category": "餐饮",
                "occurred_at": "2026-08-09T12:00:00+08:00",
                "account_id": account_id,
            },
        )
        assert denied.status_code == 404
