from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.client_api import router as client_router
from lark_ledger.client_schemas import ClientCredentialCreateRequest
from lark_ledger.config import Settings
from lark_ledger.models import AccountType, Base
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.client_auth import ClientCredentialService
from lark_ledger.services.dashboard_auth import DashboardPrincipal
from lark_ledger.services.identity import IdentityService
from lark_ledger.web_api import (
    csrf_principal,
    current_principal,
)
from lark_ledger.web_api import (
    router as web_router,
)


@pytest_asyncio.fixture
async def api_setup() -> AsyncIterator[
    tuple[FastAPI, async_sessionmaker[AsyncSession], str, DashboardPrincipal]
]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_api_transfer")
        accounts = AccountService(session)
        bank = await accounts.get_default(context)
        bank.name = "银行"
        bank.normalized_name = "银行"
        await accounts.create(
            context,
            name="钱包",
            account_type=AccountType.ASSET,
            opening_balance=Decimal("20"),
        )
        credential = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(name="api test"),
        )
        principal = DashboardPrincipal(
            session_id=uuid.uuid4(),
            user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            user_open_id="ou_api_transfer",
            display_name="test",
            avatar_url="",
            role="USER",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await session.commit()

    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.state.session_factory = factory
    app.include_router(client_router)
    app.include_router(web_router)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[csrf_principal] = lambda: principal
    yield app, factory, credential.token, principal
    await engine.dispose()


async def test_client_and_web_transfer_permissions_and_balances(api_setup) -> None:
    app, factory, token, principal = api_setup
    headers = {"Authorization": f"Bearer {token}"}
    async with factory() as session:
        accounts = await AccountService(session).list(principal.request_context)
        bank, wallet = accounts
    payload = {
        "from_account_id": str(bank.id),
        "to_account_id": str(wallet.id),
        "amount": "12.50",
        "occurred_at": "2026-08-09T12:00:00+08:00",
        "note": "资金归集",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        created = await client.post(
            "/api/client/v1/transfers",
            headers=headers | {"Idempotency-Key": "transfer-1"},
            json=payload,
        )
        assert created.status_code == 201
        transfer_id = created.json()["id"]
        replay = await client.post(
            "/api/client/v1/transfers",
            headers=headers | {"Idempotency-Key": "transfer-1"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == transfer_id

        client_detail = await client.get(f"/api/client/v1/transfers/{transfer_id}", headers=headers)
        web_detail = await client.get(f"/api/web/v1/transfers/{transfer_id}")
        assert client_detail.status_code == web_detail.status_code == 200
        assert client_detail.json()["ledger_id"] == web_detail.json()["ledger_id"]

        balance = await client.get(f"/api/client/v1/accounts/{wallet.id}/balance", headers=headers)
        assets = await client.get("/api/web/v1/assets")
        assert balance.json()["current_balance"] == "32.50"
        assert assets.json()["total_assets"] == "20.00"

        reversed_row = await client.post(f"/api/web/v1/transfers/{transfer_id}/reverse")
        assert reversed_row.status_code == 200
        duplicate = await client.post(
            f"/api/client/v1/transfers/{transfer_id}/reverse",
            headers=headers | {"Idempotency-Key": "reverse-2"},
        )
        assert duplicate.status_code == 409


async def test_api_rejects_bare_ids_outside_current_ledger(api_setup) -> None:
    app, factory, token, _ = api_setup
    headers = {"Authorization": f"Bearer {token}"}
    async with factory() as session:
        outsider = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_outside_api")
        outside_account = await AccountService(session).get_default(outsider)
        await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        response = await client.post(
            "/api/client/v1/transfers",
            headers=headers | {"Idempotency-Key": "cross-ledger"},
            json={
                "from_account_id": str(outside_account.id),
                "to_account_id": str(outside_account.id),
                "amount": "1.00",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        assert response.status_code == 404
        assert (
            await client.get(
                f"/api/client/v1/accounts/{outside_account.id}/balance", headers=headers
            )
        ).status_code == 404
