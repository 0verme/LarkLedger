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
        assert client_detail.json()["ledger_id"] == web_detail.json()["transfer"]["ledger_id"]

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


async def test_web_account_transfer_and_client_credential_lifecycle(api_setup) -> None:
    app, factory, _, principal = api_setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        listed_credentials = await client.get("/api/web/v1/client-credentials")
        assert listed_credentials.status_code == 200
        created_credential = await client.post(
            "/api/web/v1/client-credentials",
            json={
                "name": "acceptance device",
                "scopes": ["ledger:read", "ledger:write", "pending:write"],
            },
        )
        assert created_credential.status_code == 201
        credential_id = created_credential.json()["id"]
        assert created_credential.json()["token"].startswith("llv1_")

        initial = await client.get("/api/web/v1/accounts")
        assert initial.status_code == 200
        original_default = initial.json()["items"][0]["id"]
        asset = await client.post(
            "/api/web/v1/accounts",
            json={
                "name": "Web asset",
                "type": "asset",
                "opening_balance": "75.00",
            },
        )
        assert asset.status_code == 201
        asset_id = asset.json()["id"]
        liability = await client.post(
            "/api/web/v1/accounts",
            json={
                "name": "Web liability",
                "type": "liability",
                "opening_balance": "10.00",
            },
        )
        assert liability.status_code == 201
        liability_id = liability.json()["id"]
        renamed = await client.patch(
            f"/api/web/v1/accounts/{asset_id}", json={"name": "Web cash asset"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Web cash asset"
        assert (await client.get(f"/api/web/v1/accounts/{asset_id}")).status_code == 200
        assert (await client.post(f"/api/web/v1/accounts/{asset_id}/default")).status_code == 200
        archived = await client.post(f"/api/web/v1/accounts/{original_default}/archive")
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        assert (await client.get("/api/web/v1/accounts?include_archived=true")).status_code == 200
        assert (await client.get(f"/api/web/v1/accounts/{asset_id}/balance")).status_code == 200
        assets = await client.get("/api/web/v1/assets")
        assert assets.status_code == 200
        assert assets.json()["net_assets"] == "85.00"

        transfer = await client.post(
            "/api/web/v1/transfers",
            json={
                "from_account_id": asset_id,
                "to_account_id": liability_id,
                "amount": "5.00",
                "occurred_at": "2026-08-09T12:00:00+08:00",
                "note": "web transfer",
            },
        )
        assert transfer.status_code == 201
        transfer_id = transfer.json()["id"]
        assert (await client.get(f"/api/web/v1/transfers/{transfer_id}")).status_code == 200
        reversed_transfer = await client.post(f"/api/web/v1/transfers/{transfer_id}/reverse")
        assert reversed_transfer.status_code == 200
        assert reversed_transfer.json()["reversed_at"] is not None

        missing_account = uuid.uuid4()
        assert (await client.get(f"/api/web/v1/accounts/{missing_account}")).status_code == 404
        assert (
            await client.get(f"/api/web/v1/accounts/{missing_account}/balance")
        ).status_code == 404
        assert (await client.get(f"/api/web/v1/transfers/{uuid.uuid4()}")).status_code == 404

        revoked = await client.delete(f"/api/web/v1/client-credentials/{credential_id}")
        assert revoked.status_code == 204
        async with factory() as session:
            accounts = await AccountService(session).list(
                principal.request_context, include_archived=True
            )
            assert len(accounts) == 4


async def test_web_household_and_ledger_management_lifecycle(api_setup) -> None:
    app, factory, _, owner = api_setup
    async with factory() as session:
        member_context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_web_member")
        member = DashboardPrincipal(
            session_id=uuid.uuid4(),
            user_id=member_context.actor_user_id,
            ledger_id=member_context.ledger_id,
            user_open_id="ou_web_member",
            display_name="member",
            avatar_url="",
            role="USER",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        ledgers = await client.get("/api/web/v1/ledgers")
        assert ledgers.status_code == 200
        assert (await client.get("/api/web/v1/ledgers/current")).status_code == 200
        new_ledger = await client.post(
            "/api/web/v1/ledgers", json={"name": "Web acceptance ledger"}
        )
        assert new_ledger.status_code == 201
        ledger_id = new_ledger.json()["id"]
        renamed_ledger = await client.patch(
            f"/api/web/v1/ledgers/{ledger_id}", json={"name": "Web renamed ledger"}
        )
        assert renamed_ledger.status_code == 200
        assert renamed_ledger.json()["name"] == "Web renamed ledger"
        assert (await client.post(f"/api/web/v1/ledgers/{ledger_id}/default")).status_code == 200

        household = await client.post(
            "/api/web/v1/households", json={"name": "Web acceptance family"}
        )
        assert household.status_code == 201
        household_id = household.json()["id"]
        assert (await client.get("/api/web/v1/households")).status_code == 200
        detail = await client.get(f"/api/web/v1/households/{household_id}")
        assert detail.status_code == 200
        renamed = await client.patch(
            f"/api/web/v1/households/{household_id}",
            json={"name": "Web renamed family"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Web renamed family"
        assert (
            await client.get(f"/api/web/v1/households/{household_id}/members")
        ).status_code == 200
        invitation = await client.post(
            f"/api/web/v1/households/{household_id}/invitations",
            json={"target": "ou_web_member"},
        )
        assert invitation.status_code == 201
        invitation_id = invitation.json()["id"]

        app.dependency_overrides[current_principal] = lambda: member
        app.dependency_overrides[csrf_principal] = lambda: member
        invitations = await client.get("/api/web/v1/household-invitations")
        assert invitations.status_code == 200
        assert invitations.json()[0]["id"] == invitation_id
        accepted = await client.post(f"/api/web/v1/household-invitations/{invitation_id}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"
        assert (await client.get(f"/api/web/v1/households/{household_id}")).status_code == 200
        assert (
            await client.post(f"/api/web/v1/households/{household_id}/leave")
        ).status_code == 204

        app.dependency_overrides[current_principal] = lambda: owner
        app.dependency_overrides[csrf_principal] = lambda: owner
        missing = uuid.uuid4()
        assert (await client.get(f"/api/web/v1/households/{missing}")).status_code == 404
        assert (
            await client.patch(f"/api/web/v1/households/{missing}", json={"name": "missing"})
        ).status_code == 404
        assert (await client.get(f"/api/web/v1/households/{missing}/members")).status_code == 404


async def test_web_create_entry_with_account_and_transfer_list_and_detail(
    api_setup,
) -> None:
    app, factory, token, principal = api_setup
    async with factory() as session:
        accounts = await AccountService(session).list(principal.request_context)
        bank, wallet = accounts
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        # Web create entry with an explicit account.
        created = await client.post(
            "/api/web/v1/entries",
            headers={"Idempotency-Key": "transfer-web-create"},
            json={
                "amount": "42.00",
                "direction": "expense",
                "category": "餐饮",
                "note": "午餐",
                "occurred_at": "2026-08-09T12:00:00+08:00",
                "account_id": str(wallet.id),
            },
        )
        assert created.status_code == 201
        entry = created.json()["entry"]
        assert entry["account_id"] == str(wallet.id)
        assert entry["account_name"] == "钱包"
        short_id = entry["short_id"]

        # Web create with an unknown account is rejected.
        bad = await client.post(
            "/api/web/v1/entries",
            headers={"Idempotency-Key": "transfer-web-bad-account"},
            json={
                "amount": "1.00",
                "direction": "expense",
                "category": "餐饮",
                "occurred_at": "2026-08-09T12:00:00+08:00",
                "account_id": str(uuid.uuid4()),
            },
        )
        assert bad.status_code == 404

        # Web PATCH moves the entry's account.
        patched = await client.patch(
            f"/api/web/v1/entries/{short_id}",
            json={"expected_updated_at": entry["updated_at"], "account_id": str(bank.id)},
        )
        assert patched.status_code == 200
        assert patched.json()["entry"]["account_id"] == str(bank.id)
        assert patched.json()["revisions"][0]["after"]["account_id"] == str(bank.id)

        # Transfer list + detail with revisions.
        transfer = await client.post(
            "/api/web/v1/transfers",
            json={
                "from_account_id": str(bank.id),
                "to_account_id": str(wallet.id),
                "amount": "10.00",
                "occurred_at": "2026-08-09T12:00:00+08:00",
                "note": "归集",
            },
        )
        assert transfer.status_code == 201
        transfer_id = transfer.json()["id"]
        listing = await client.get("/api/web/v1/transfers?page=1&page_size=20")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["id"] == transfer_id

        detail = await client.get(f"/api/web/v1/transfers/{transfer_id}")
        assert detail.status_code == 200
        assert detail.json()["transfer"]["id"] == transfer_id
        assert detail.json()["transfer"]["from_account_id"] == str(bank.id)
        # A fresh transfer has no audit rows yet; reversing records one.
        assert detail.json()["revisions"] == []
        reversed_row = await client.post(f"/api/web/v1/transfers/{transfer_id}/reverse")
        assert reversed_row.status_code == 200
        detail = await client.get(f"/api/web/v1/transfers/{transfer_id}")
        assert detail.json()["revisions"][0]["change_type"] == "reverse"
