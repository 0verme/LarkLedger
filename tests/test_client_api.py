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
from lark_ledger.models import (
    Base,
    ClientCredential,
    ClientIdempotencyRecord,
    Direction,
    LedgerEntry,
    PendingCommand,
)
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.client_auth import (
    ClientAuthenticationError,
    ClientCredentialService,
)
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.pending import PendingPreview, PendingPreviewItem


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
            request=ClientCredentialCreateRequest(name="test device", expires_at=expires_at),
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
    service = ClientCredentialService(client_factory, currency="CNY", timezone="Asia/Shanghai")
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


async def test_client_api_budget_period_and_total(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _credential(client_factory, subject="ou_budget_client")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        headers = {"Authorization": f"Bearer {token}"}
        write_headers = headers | {"Idempotency-Key": "budget-total"}

        total = await client.put(
            "/api/client/v1/budgets/total?period=2026-08",
            headers=write_headers,
            json={"amount": "12000"},
        )
        assert total.status_code == 200
        assert total.json()["total_limit_set"] is True
        assert total.json()["total_budget"] == "12000.00"
        assert total.json()["period"] == "2026-08"

        category = await client.put(
            "/api/client/v1/budgets/food?period=2026-08",
            headers=headers | {"Idempotency-Key": "budget-cat"},
            json={"amount": "3000"},
        )
        assert category.status_code == 200
        items = {item["category"]: item for item in category.json()["items"]}
        assert items["food"]["amount"] == "3000.00"

        listed = await client.get("/api/client/v1/budgets?period=2026-08", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["total_budget"] == "12000.00"
        assert listed.json()["allocated"] == "3000.00"
        assert listed.json()["unallocated"] == "9000.00"

        bad = await client.put(
            "/api/client/v1/budgets/total?period=2026-13",
            headers=headers | {"Idempotency-Key": "budget-bad"},
            json={"amount": "1"},
        )
        assert bad.status_code == 422

        removed = await client.delete(
            "/api/client/v1/budgets/total?period=2026-08",
            headers=headers | {"Idempotency-Key": "budget-total-delete"},
        )
        assert removed.status_code == 200
        assert removed.json()["total_limit_set"] is False


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
    error = response.json()["error"]
    assert error["code"] == "authentication_required"
    assert error["message"] == "valid bearer credential required"
    # Stable v1 envelope carries a request_id for traceability (§32).
    assert isinstance(error["request_id"], str) and error["request_id"]
    schema = app.openapi()
    assert "/api/client/v1/entries" in schema["paths"]
    assert "/api/v1/transactions" in schema["paths"]
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
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
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
    service = ClientCredentialService(client_factory, currency="CNY", timezone="Asia/Shanghai")
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


async def test_client_account_entry_balance_and_reporting_lifecycle(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _credential(client_factory, subject="ou_full_finance")
    headers = {"Authorization": f"Bearer {token}"}
    write_headers = headers | {"Idempotency-Key": "placeholder"}
    occurred_at = "2026-08-09T12:00:00+08:00"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        me = await client.get("/api/client/v1/me", headers=headers)
        assert me.status_code == 200

        ledgers = await client.get("/api/client/v1/ledgers", headers=headers)
        assert ledgers.status_code == 200
        original_id = ledgers.json()["items"][0]["id"]
        current = await client.get("/api/client/v1/ledgers/current", headers=headers)
        assert current.status_code == 200

        created_ledger = await client.post(
            "/api/client/v1/ledgers",
            headers=write_headers | {"Idempotency-Key": "ledger-create"},
            json={"name": "Acceptance ledger"},
        )
        assert created_ledger.status_code == 201
        ledger_id = created_ledger.json()["id"]
        renamed_ledger = await client.patch(
            f"/api/client/v1/ledgers/{ledger_id}",
            headers=write_headers | {"Idempotency-Key": "ledger-rename"},
            json={"name": "Acceptance renamed"},
        )
        assert renamed_ledger.status_code == 200
        assert renamed_ledger.json()["name"] == "Acceptance renamed"
        assert (
            await client.post(
                f"/api/client/v1/ledgers/{ledger_id}/default",
                headers=write_headers | {"Idempotency-Key": "ledger-default"},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/api/client/v1/ledgers/{ledger_id}/select",
                headers=write_headers | {"Idempotency-Key": "ledger-select"},
            )
        ).status_code == 200

        account_rows = await client.get("/api/client/v1/accounts", headers=headers)
        default_id = account_rows.json()["items"][0]["id"]
        wallet = await client.post(
            "/api/client/v1/accounts",
            headers=write_headers | {"Idempotency-Key": "account-wallet"},
            json={
                "name": "Wallet",
                "type": "asset",
                "opening_balance": "100.00",
                "subtype": "wallet",
                "provider": "test",
            },
        )
        assert wallet.status_code == 201
        wallet_id = wallet.json()["id"]
        liability = await client.post(
            "/api/client/v1/accounts",
            headers=write_headers | {"Idempotency-Key": "account-liability"},
            json={
                "name": "Credit card",
                "type": "liability",
                "opening_balance": "20.00",
            },
        )
        assert liability.status_code == 201
        liability_id = liability.json()["id"]
        renamed = await client.patch(
            f"/api/client/v1/accounts/{wallet_id}",
            headers=write_headers | {"Idempotency-Key": "account-rename"},
            json={"name": "Daily wallet"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Daily wallet"
        assert (
            await client.get(f"/api/client/v1/accounts/{wallet_id}", headers=headers)
        ).status_code == 200
        assert (
            await client.post(
                f"/api/client/v1/accounts/{wallet_id}/default",
                headers=write_headers | {"Idempotency-Key": "account-default"},
            )
        ).status_code == 200
        archived = await client.post(
            f"/api/client/v1/accounts/{default_id}/archive",
            headers=write_headers | {"Idempotency-Key": "account-archive"},
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        with_archived = await client.get(
            "/api/client/v1/accounts?include_archived=true", headers=headers
        )
        assert any(item["id"] == default_id for item in with_archived.json()["items"])

        income = await client.post(
            "/api/client/v1/entries",
            headers=write_headers | {"Idempotency-Key": "entry-income"},
            json={
                "amount": "50.00",
                "direction": "income",
                "category": "salary",
                "note": "acceptance income",
                "occurred_at": occurred_at,
                "account_id": wallet_id,
            },
        )
        assert income.status_code == 201
        short_id = income.json()["resource"]["short_id"]
        expense = await client.post(
            "/api/client/v1/entries",
            headers=write_headers | {"Idempotency-Key": "entry-expense"},
            json={
                "amount": "5.00",
                "direction": "expense",
                "category": "food",
                "occurred_at": occurred_at,
                "account_id": wallet_id,
            },
        )
        assert expense.status_code == 201

        balance = await client.get(f"/api/client/v1/accounts/{wallet_id}/balance", headers=headers)
        assert balance.status_code == 200
        assert balance.json()["current_balance"] == "145.00"
        assets = await client.get("/api/client/v1/assets", headers=headers)
        assert assets.status_code == 200
        assert assets.json()["total_assets"] == "145.00"
        assert assets.json()["total_liabilities"] == "20.00"
        assert assets.json()["net_assets"] == "125.00"
        assert (
            await client.get(f"/api/client/v1/accounts/{liability_id}/balance", headers=headers)
        ).status_code == 200

        assert (await client.get("/api/client/v1/dashboard", headers=headers)).status_code == 200
        listed = await client.get(
            "/api/client/v1/entries",
            headers=headers,
            params={"start": "2026-08-01T00:00:00Z", "sort": "amount", "order": "asc"},
        )
        assert listed.status_code == 200
        detail = await client.get(f"/api/client/v1/entries/{short_id}", headers=headers)
        assert detail.status_code == 200
        version = detail.json()["entry"]["updated_at"]
        updated = await client.patch(
            f"/api/client/v1/entries/{short_id}",
            headers=write_headers | {"Idempotency-Key": "entry-update"},
            json={"expected_updated_at": version, "amount": "60.00", "note": "updated"},
        )
        assert updated.status_code == 200
        deleted = await client.request(
            "DELETE",
            f"/api/client/v1/entries/{short_id}",
            headers=write_headers | {"Idempotency-Key": "entry-delete"},
            json={"expected_updated_at": updated.json()["entry"]["updated_at"]},
        )
        assert deleted.status_code == 200
        restored = await client.post(
            f"/api/client/v1/entries/{short_id}/restore",
            headers=write_headers | {"Idempotency-Key": "entry-restore"},
            json={"expected_updated_at": deleted.json()["entry"]["updated_at"]},
        )
        assert restored.status_code == 200
        assert restored.json()["entry"]["deleted_at"] is None

        budget = await client.put(
            "/api/client/v1/budgets/food",
            headers=write_headers | {"Idempotency-Key": "budget-set"},
            json={"amount": "200.00", "currency": "CNY"},
        )
        assert budget.status_code == 200
        assert (await client.get("/api/client/v1/budgets", headers=headers)).status_code == 200
        assert (
            await client.delete(
                "/api/client/v1/budgets/food",
                headers=write_headers | {"Idempotency-Key": "budget-delete"},
            )
        ).status_code == 200
        assert (
            await client.get(
                "/api/client/v1/analytics",
                headers=headers,
                params={"start_date": "2026-08-01", "end_date": "2026-08-31"},
            )
        ).status_code == 200
        report = await client.get(
            "/api/client/v1/reports",
            headers=headers,
            params={
                "start": "2026-08-01T00:00:00Z",
                "end": "2026-08-31T23:59:59Z",
            },
        )
        assert report.status_code == 200
        exported = await client.get(
            "/api/client/v1/exports.csv",
            headers=headers,
            params={
                "start": "2026-08-01T00:00:00Z",
                "end": "2026-08-31T23:59:59Z",
                "include_deleted": "true",
            },
        )
        assert exported.status_code == 200
        assert "text/csv" in exported.headers["content-type"]

        assert (
            await client.post(
                f"/api/client/v1/ledgers/{original_id}/select",
                headers=write_headers | {"Idempotency-Key": "ledger-select-back"},
            )
        ).status_code == 200


async def test_client_household_invitation_membership_and_leave_lifecycle(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_token, _ = await _credential(client_factory, subject="ou_household_api_owner")
    member_token, _ = await _credential(client_factory, subject="ou_household_api_member")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        household = await client.post(
            "/api/client/v1/households",
            headers=owner_headers | {"Idempotency-Key": "household-create"},
            json={"name": "Acceptance family"},
        )
        assert household.status_code == 201
        household_id = household.json()["id"]
        assert (
            await client.get("/api/client/v1/households", headers=owner_headers)
        ).status_code == 200
        renamed = await client.patch(
            f"/api/client/v1/households/{household_id}",
            headers=owner_headers | {"Idempotency-Key": "household-rename"},
            json={"name": "Acceptance household"},
        )
        assert renamed.status_code == 200
        assert (
            await client.get(f"/api/client/v1/households/{household_id}", headers=owner_headers)
        ).status_code == 200
        assert (
            await client.get(
                f"/api/client/v1/households/{household_id}/members", headers=owner_headers
            )
        ).status_code == 200

        invitation = await client.post(
            f"/api/client/v1/households/{household_id}/invitations",
            headers=owner_headers | {"Idempotency-Key": "household-invite"},
            json={"target": "ou_household_api_member"},
        )
        assert invitation.status_code == 201
        invitation_id = invitation.json()["id"]
        member_invitations = await client.get(
            "/api/client/v1/household-invitations", headers=member_headers
        )
        assert member_invitations.status_code == 200
        assert member_invitations.json()[0]["id"] == invitation_id
        accepted = await client.post(
            f"/api/client/v1/household-invitations/{invitation_id}/accept",
            headers=member_headers | {"Idempotency-Key": "household-accept"},
        )
        assert accepted.status_code == 200
        members = await client.get(
            f"/api/client/v1/households/{household_id}/members", headers=owner_headers
        )
        assert len(members.json()) == 2
        left = await client.post(
            f"/api/client/v1/households/{household_id}/leave",
            headers=member_headers | {"Idempotency-Key": "household-leave"},
        )
        assert left.status_code == 200
        assert left.json()["message"] == "household left"


async def test_client_pending_query_confirm_cancel_and_frozen_scope(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    command = ParsedCommand(
        action=Action.CREATE,
        amount="32.00",
        direction=Direction.EXPENSE,
        category="acceptance",
        note="frozen pending",
        occurred_at=now,
    )

    def preview(code: str) -> dict[str, object]:
        return PendingPreview(
            code=code,
            display_code=f"#C-{code[1:]}",
            entries_total=1,
            expense_count=1,
            expense_total="32.00",
            income_total="0.00",
            currency="CNY",
            risk_reason="acceptance",
            expires_at=(now + timedelta(hours=1)).isoformat(),
            items=[
                PendingPreviewItem(
                    index=None,
                    direction="expense",
                    amount="32.00",
                    currency="CNY",
                    category="acceptance",
                    occurred_at="2026-08-09 12:00",
                    note="frozen pending",
                )
            ],
        ).as_json()

    async with client_factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_client_pending")
        credential = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(
                name="pending device",
                scopes=["ledger:read", "ledger:write", "pending:write"],
            ),
        )
        for code in ("CA83F2", "CB83F2"):
            session.add(
                PendingCommand(
                    confirmation_code=code,
                    user_open_id="ou_client_pending",
                    actor_user_id=context.actor_user_id,
                    ledger_id=context.ledger_id,
                    source_message_id=f"om_{code}",
                    transport="feishu",
                    source_type="image",
                    command_type=command.action.value,
                    payload_version=1,
                    payload_json=command.model_dump(mode="json"),
                    preview_json=preview(code),
                    risk_reason="acceptance",
                    status="pending",
                    expires_at=now + timedelta(hours=1),
                )
            )
        await session.commit()

    headers = {"Authorization": f"Bearer {credential.token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        listed = await client.get("/api/client/v1/pending", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["total"] == 2
        detail = await client.get("/api/client/v1/pending/CA83F2", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["pending"]["confirmation_id"] == "#C-A83F2"
        assert (await client.get("/api/client/v1/pending/NO!", headers=headers)).status_code == 404
        assert (
            await client.post(
                "/api/client/v1/pending/NO!/confirm",
                headers=headers | {"Idempotency-Key": "invalid-pending-confirm"},
            )
        ).status_code == 404

        confirmed = await client.post(
            "/api/client/v1/pending/CA83F2/confirm",
            headers=headers | {"Idempotency-Key": "pending-confirm"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["pending"]["pending"]["status"] == "executed"
        replayed = await client.post(
            "/api/client/v1/pending/CA83F2/confirm",
            headers=headers | {"Idempotency-Key": "pending-confirm"},
        )
        assert replayed.status_code == 200
        cancelled = await client.post(
            "/api/client/v1/pending/CB83F2/cancel",
            headers=headers | {"Idempotency-Key": "pending-cancel"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["pending"]["pending"]["status"] == "cancelled"

    async with client_factory() as session:
        entry = await session.scalar(
            select(LedgerEntry).where(LedgerEntry.ledger_id == context.ledger_id)
        )
        assert entry is not None
        assert entry.amount == command.amount


async def test_client_financial_error_envelopes_and_write_scope(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _credential(client_factory, subject="ou_error_branches")
    headers = {"Authorization": f"Bearer {token}"}
    async with client_factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_read_only")
        read_only = await ClientCredentialService.create(
            session,
            user_id=context.actor_user_id,
            current_ledger_id=context.ledger_id,
            request=ClientCredentialCreateRequest(name="read only", scopes=["ledger:read"]),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        denied = await client.post(
            "/api/client/v1/accounts",
            headers={
                "Authorization": f"Bearer {read_only.token}",
                "Idempotency-Key": "denied-write",
            },
            json={"name": "Denied", "type": "asset"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "permission_denied"

        ledger = await client.post(
            "/api/client/v1/ledgers",
            headers=headers | {"Idempotency-Key": "error-ledger-create"},
            json={"name": "Error ledger"},
        )
        assert ledger.status_code == 201
        duplicate = await client.post(
            "/api/client/v1/ledgers",
            headers=headers | {"Idempotency-Key": "error-ledger-duplicate"},
            json={"name": "Error ledger"},
        )
        assert duplicate.status_code == 409
        missing = uuid.uuid4()
        assert (
            await client.patch(
                f"/api/client/v1/ledgers/{missing}",
                headers=headers | {"Idempotency-Key": "missing-ledger-rename"},
                json={"name": "Missing"},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/client/v1/ledgers/{missing}/default",
                headers=headers | {"Idempotency-Key": "missing-ledger-default"},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/client/v1/ledgers/{missing}/select",
                headers=headers | {"Idempotency-Key": "missing-ledger-select"},
            )
        ).status_code == 404

        accounts = await client.get("/api/client/v1/accounts", headers=headers)
        default_id = accounts.json()["items"][0]["id"]
        assert (
            await client.post(
                f"/api/client/v1/accounts/{default_id}/archive",
                headers=headers | {"Idempotency-Key": "archive-default"},
            )
        ).status_code == 409
        assert (
            await client.post(
                "/api/client/v1/accounts",
                headers=headers | {"Idempotency-Key": "duplicate-account"},
                json={"name": accounts.json()["items"][0]["name"], "type": "asset"},
            )
        ).status_code == 409
        assert (
            await client.get(f"/api/client/v1/accounts/{missing}", headers=headers)
        ).status_code == 404
        assert (
            await client.get(f"/api/client/v1/accounts/{missing}/balance", headers=headers)
        ).status_code == 404

        invalid_transfer = await client.post(
            "/api/client/v1/transfers",
            headers=headers | {"Idempotency-Key": "same-account-transfer"},
            json={
                "from_account_id": default_id,
                "to_account_id": default_id,
                "amount": "1.00",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        assert invalid_transfer.status_code == 409
        assert (
            await client.get(f"/api/client/v1/transfers/{missing}", headers=headers)
        ).status_code == 404
        assert (
            await client.post(
                f"/api/client/v1/transfers/{missing}/reverse",
                headers=headers | {"Idempotency-Key": "missing-transfer-reverse"},
            )
        ).status_code == 404
        assert (
            await client.get("/api/client/v1/entries/INVALID", headers=headers)
        ).status_code == 404


async def test_client_patch_entry_account_moves_binding_and_rejects_foreign(
    client_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _credential(client_factory, subject="ou_patch_account")
    other_token, _ = await _credential(client_factory, subject="ou_patch_other")
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(client_factory)),
        base_url="http://ledger.test",
    ) as client:
        me = await client.get("/api/client/v1/me", headers=headers)
        ledger_id = me.json()["ledger_id"]
        defaults = await client.get("/api/client/v1/accounts", headers=headers)
        default_id = defaults.json()["items"][0]["id"]
        wallet = await client.post(
            "/api/client/v1/accounts",
            headers=headers | {"Idempotency-Key": "patch-wallet"},
            json={"name": "支付宝", "type": "asset", "opening_balance": "0"},
        )
        assert wallet.status_code == 201
        wallet_id = wallet.json()["id"]

        created = await client.post(
            "/api/client/v1/entries",
            headers=headers | {"Idempotency-Key": "patch-entry"},
            json={
                "amount": "50.00",
                "direction": "expense",
                "category": "餐饮",
                "occurred_at": "2026-08-09T12:00:00+08:00",
            },
        )
        assert created.status_code == 201
        short_id = created.json()["resource"]["short_id"]
        assert created.json()["resource"]["account_id"] == default_id

        detail = await client.get(f"/api/client/v1/entries/{short_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["entry"]["account_id"] == default_id

        updated = await client.patch(
            f"/api/client/v1/entries/{short_id}",
            headers=headers | {"Idempotency-Key": "patch-account"},
            json={
                "expected_updated_at": detail.json()["entry"]["updated_at"],
                "account_id": wallet_id,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["entry"]["account_id"] == wallet_id
        assert updated.json()["entry"]["account_name"] == "支付宝"
        assert updated.json()["revisions"][0]["change_type"] == "update"
        assert updated.json()["revisions"][0]["before"]["account_id"] == default_id
        assert updated.json()["revisions"][0]["after"]["account_id"] == wallet_id

        # Balance moved between the two accounts.
        default_balance = await client.get(
            f"/api/client/v1/accounts/{default_id}/balance", headers=headers
        )
        assert default_balance.json()["current_balance"] == "0.00"
        wallet_balance = await client.get(
            f"/api/client/v1/accounts/{wallet_id}/balance", headers=headers
        )
        assert wallet_balance.json()["current_balance"] == "-50.00"

        # A foreign-ledger account is rejected (404, nothing written).
        foreign_me = await client.get(
            "/api/client/v1/me", headers={"Authorization": f"Bearer {other_token}"}
        )
        foreign_ledger = foreign_me.json()["ledger_id"]
        assert foreign_ledger != ledger_id
        foreign_accounts = await client.get(
            "/api/client/v1/accounts", headers={"Authorization": f"Bearer {other_token}"}
        )
        foreign_account_id = foreign_accounts.json()["items"][0]["id"]
        rejected = await client.patch(
            f"/api/client/v1/entries/{short_id}",
            headers=headers | {"Idempotency-Key": "patch-foreign"},
            json={
                "expected_updated_at": updated.json()["entry"]["updated_at"],
                "account_id": foreign_account_id,
            },
        )
        assert rejected.status_code == 404

        # Unknown fields are rejected instead of silently ignored (extra=forbid).
        unknown = await client.patch(
            f"/api/client/v1/entries/{short_id}",
            headers=headers | {"Idempotency-Key": "patch-unknown"},
            json={
                "expected_updated_at": updated.json()["entry"]["updated_at"],
                "amount": "99",
                "accountId": wallet_id,
            },
        )
        assert unknown.status_code == 422
