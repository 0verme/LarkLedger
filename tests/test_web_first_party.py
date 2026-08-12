"""P38 — First-party Web Client API tests (WEB01–WEB15).

These exercise the browser-facing contract exactly as the Web client uses it:
Human Session (P37) + CSRF double-submit + Idempotency-Key, with every write
routed through ``ClientApplicationService`` (never a raw repository query from
the adapter).

Matrix:
    WEB01 session authenticated
    WEB02 ledger list
    WEB03 select authorized ledger
    WEB04 unauthorized ledger → 404
    WEB05 transaction list
    WEB06 transaction create
    WEB07 idempotency replay (same key → still 1 row)
    WEB08 update
    WEB09 delete
    WEB10 restore
    WEB11 accounts
    WEB12 private account isolation (404 for other household members)
    WEB13 household shared ledger + privacy
    WEB14 invalid CSRF → 403
    WEB15 invalid session → 401
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import (
    Base,
    LedgerEntry,
)
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    DashboardAuthService,
)
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.web_api import _auth_service, router


def settings() -> Settings:
    return Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="test-only-secret-that-is-long-enough-123456",
        dashboard_cookie_secure=False,
        dashboard_admin_open_ids="",
        lark_app_id="cli_test",
        lark_app_secret="app-secret",
        pending_expires_seconds=3600,
        currency="CNY",
        timezone="Asia/Shanghai",
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


async def _client(
    factory: async_sessionmaker[AsyncSession],
    user: str,
) -> tuple[httpx.AsyncClient, str]:
    auth = DashboardAuthService(settings(), factory)
    created = await auth.create_session({"open_id": user, "name": user, "avatar_url": ""})
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


def _csrf(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token}


def _entry_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "amount": "28.00",
        "direction": "expense",
        "category": "餐饮",
        "note": "午餐",
        "occurred_at": "2026-08-10T04:30:00+08:00",
        "account_id": None,
    }
    body.update(overrides)
    return body


async def _entry_count(factory: async_sessionmaker[AsyncSession], ledger_id: uuid.UUID) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.ledger_id == ledger_id)
            )
            or 0
        )


# ---------------------------------------------------------------------------
# WEB01–WEB04 — session, ledgers, ledger selection
# ---------------------------------------------------------------------------


async def test_web01_session_authenticated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf_token = await _client(factory, "ou_a")
    async with client:
        response = await client.get("/api/web/v1/me")
        assert response.status_code == 200
        me = response.json()
        assert me["open_id"] == "ou_a"
        assert me["role"] == "USER"


async def test_web02_ledger_list_and_current(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf_token = await _client(factory, "ou_a")
    async with client:
        listed = await client.get("/api/web/v1/ledgers")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) >= 1
        personal = [item for item in items if item["kind"] == "personal"]
        assert personal, "every user owns a personal ledger"
        current = await client.get("/api/web/v1/ledgers/current")
        assert current.status_code == 200
        assert any(item["is_current"] for item in items)


async def test_web03_select_authorized_ledger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        listed = await client.get("/api/web/v1/ledgers")
        target = listed.json()["items"][0]
        selected = await client.post(
            f"/api/web/v1/ledgers/{target['id']}/select", headers=_csrf(csrf_token)
        )
        assert selected.status_code == 200
        assert selected.json()["is_current"] is True
        # The selection is persisted on the server-side session row, so a
        # refresh restores it (P38 §66/§67 — no SPA memory required).
        current = await client.get("/api/web/v1/ledgers/current")
        assert current.json()["id"] == target["id"]


async def test_web04_unauthorized_ledger_is_404(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_client, owner_csrf = await _client(factory, "ou_owner")
    async with owner_client:
        listed = await owner_client.get("/api/web/v1/ledgers")
        owner_personal = [item for item in listed.json()["items"] if item["kind"] == "personal"][0]
    outsider_client, outsider_csrf = await _client(factory, "ou_outsider")
    async with outsider_client:
        # An outsider cannot read the owner's ledger nor select it: 404, not 403
        # (never reveals existence).
        rejected = await outsider_client.post(
            f"/api/web/v1/ledgers/{owner_personal['id']}/select",
            headers=_csrf(outsider_csrf),
        )
        assert rejected.status_code == 404


# ---------------------------------------------------------------------------
# WEB05–WEB10 — transactions lifecycle
# ---------------------------------------------------------------------------


async def test_web05_transaction_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "web05-key"},
            json=_entry_body(note="列表测试"),
        )
        listed = await client.get("/api/web/v1/entries")
        assert listed.status_code == 200
        page = listed.json()
        assert page["items"], "the created entry is listed"
        assert page["page"] == 1
        assert all(item["note"] != "" for item in page["items"])


async def test_web06_transaction_create_expense_and_income(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        expense = await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "web06-expense"},
            json=_entry_body(),
        )
        assert expense.status_code == 201
        expense_entry = expense.json()["entry"]
        assert expense_entry["direction"] == "expense"
        assert expense_entry["amount"] == "28.00"
        income = await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "web06-income"},
            json=_entry_body(
                amount="18000.00",
                direction="income",
                category="工资",
                note="发薪",
            ),
        )
        assert income.status_code == 201
        income_entry = income.json()["entry"]
        assert income_entry["direction"] == "income"
        assert income_entry["amount"] == "18000.00"
        assert income_entry["short_id"] != expense_entry["short_id"]


async def test_web07_idempotency_replay_creates_one_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        listed = await client.get("/api/web/v1/ledgers")
        ledger_id = listed.json()["items"][0]["id"]
        headers = {**_csrf(csrf_token), "Idempotency-Key": "web07-same-key"}
        first = await client.post("/api/web/v1/entries", headers=headers, json=_entry_body())
        assert first.status_code == 201
        # Simulate a browser retry after a timeout: identical key + body.
        replay = await client.post("/api/web/v1/entries", headers=headers, json=_entry_body())
        assert replay.status_code == 201
        assert replay.json()["entry"]["short_id"] == first.json()["entry"]["short_id"]
        assert await _entry_count(factory, uuid.UUID(ledger_id)) == 1
        # A different key with the same body is a brand-new entry.
        different = await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "web07-other-key"},
            json=_entry_body(),
        )
        assert different.status_code == 201
        assert await _entry_count(factory, uuid.UUID(ledger_id)) == 2


async def test_web07b_idempotency_key_conflict_is_409(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        headers = {**_csrf(csrf_token), "Idempotency-Key": "web07b-key"}
        await client.post("/api/web/v1/entries", headers=headers, json=_entry_body())
        conflict = await client.post(
            "/api/web/v1/entries",
            headers=headers,
            json=_entry_body(amount="99.00"),
        )
        assert conflict.status_code == 409


async def test_web08_update_entry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        created = await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "web08-create"},
            json=_entry_body(note="待修改"),
        )
        entry = created.json()["entry"]
        updated = await client.patch(
            f"/api/web/v1/entries/{entry['short_id']}",
            headers=_csrf(csrf_token),
            json={
                "expected_updated_at": entry["updated_at"],
                "amount": "30.00",
                "direction": "expense",
                "category": "餐饮",
                "note": "午餐涨价了",
                "account_id": None,
            },
        )
        assert updated.status_code == 200
        after = updated.json()["entry"]
        assert after["amount"] == "30.00"
        assert after["note"] == "午餐涨价了"
        # The revision timeline records the change.
        assert any(revision["change_type"] == "update" for revision in updated.json()["revisions"])


async def test_web09_delete_and_web10_restore_entry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        created = await client.post(
            "/api/web/v1/entries",
            headers={**_csrf(csrf_token), "Idempotency-Key": "web09-create"},
            json=_entry_body(),
        )
        entry = created.json()["entry"]
        assert entry["deleted_at"] is None
        deleted = await client.request(
            "DELETE",
            f"/api/web/v1/entries/{entry['short_id']}",
            headers=_csrf(csrf_token),
            json={"expected_updated_at": entry["updated_at"]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["entry"]["deleted_at"] is not None
        restored = await client.post(
            f"/api/web/v1/entries/{entry['short_id']}/restore",
            headers=_csrf(csrf_token),
            json={"expected_updated_at": deleted.json()["entry"]["updated_at"]},
        )
        assert restored.status_code == 200
        assert restored.json()["entry"]["deleted_at"] is None


# ---------------------------------------------------------------------------
# WEB11–WEB12 — accounts + private isolation
# ---------------------------------------------------------------------------


async def test_web11_accounts_list_and_balance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, csrf_token = await _client(factory, "ou_a")
    async with client:
        created = await client.post(
            "/api/web/v1/accounts",
            headers=_csrf(csrf_token),
            json={"name": "招商银行", "type": "asset", "opening_balance": "1000.00"},
        )
        assert created.status_code == 201
        account = created.json()
        assert account["name"] == "招商银行"
        listed = await client.get("/api/web/v1/accounts")
        assert listed.status_code == 200
        assert any(item["id"] == account["id"] for item in listed.json()["items"])
        balance = await client.get(f"/api/web/v1/accounts/{account['id']}/balance")
        assert balance.status_code == 200
        assert balance.json()["account_id"] == account["id"]


async def test_web12_private_account_isolation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Journey C — B (household member) must not see or reach A's private
    account: list hides it, direct GET by id is a 404."""
    owner_client, owner_csrf = await _client(factory, "ou_a")
    private_id: str | None = None
    async with owner_client:
        private = await owner_client.post(
            "/api/web/v1/accounts",
            headers=_csrf(owner_csrf),
            json={
                "name": "私密钱包",
                "type": "cash",
                "opening_balance": "500.00",
                "visibility": "private",
            },
        )
        assert private.status_code == 201
        private_id = private.json()["id"]
    member_client, _member_csrf = await _client(factory, "ou_b")
    async with member_client:
        listed = await member_client.get("/api/web/v1/accounts")
        assert listed.status_code == 200
        assert private_id not in {item["id"] for item in listed.json()["items"]}, (
            "B's account list must not leak A's private account"
        )
        direct = await member_client.get(f"/api/web/v1/accounts/{private_id}")
        assert direct.status_code == 404


# ---------------------------------------------------------------------------
# WEB13 — household shared ledger
# ---------------------------------------------------------------------------


async def _household(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, uuid.UUID]:
    """Bootstrap owner (A) + member (B) + shared household; return ledger ids."""
    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_a", display_name="A")
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_b", display_name="B")
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "测试家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_b")
        await manager.accept(member.actor_user_id, invitation.public_id)
        await session.commit()
        return {
            "owner_personal": owner.ledger_id,
            "home": home.ledger.id,
        }


async def test_web13_household_shared_ledger_and_privacy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Journey B + C — both members book into the shared ledger; A's private
    account stays invisible to B; B cannot select A's personal ledger."""
    ids = await _household(factory)
    owner_client, owner_csrf = await _client(factory, "ou_a")
    async with owner_client:
        selected = await owner_client.post(
            f"/api/web/v1/ledgers/{ids['home']}/select", headers=_csrf(owner_csrf)
        )
        assert selected.status_code == 200
        created = await owner_client.post(
            "/api/web/v1/entries",
            headers={**_csrf(owner_csrf), "Idempotency-Key": "web13-shared"},
            json=_entry_body(note="家庭支出"),
        )
        assert created.status_code == 201
        shared_short_id = created.json()["entry"]["short_id"]
        private_account = await owner_client.post(
            "/api/web/v1/accounts",
            headers=_csrf(owner_csrf),
            json={
                "name": "家庭里的私密账户",
                "type": "asset",
                "opening_balance": "0.00",
                "visibility": "private",
            },
        )
        assert private_account.status_code == 201
        private_account_id = private_account.json()["id"]
    member_client, member_csrf = await _client(factory, "ou_b")
    async with member_client:
        member_select = await member_client.post(
            f"/api/web/v1/ledgers/{ids['home']}/select", headers=_csrf(member_csrf)
        )
        assert member_select.status_code == 200
        listed = await member_client.get("/api/web/v1/entries")
        assert listed.status_code == 200
        assert any(item["short_id"] == shared_short_id for item in listed.json()["items"]), (
            "B sees the shared household transaction"
        )
        accounts = await member_client.get("/api/web/v1/accounts")
        assert private_account_id not in {item["id"] for item in accounts.json()["items"]}
        direct = await member_client.get(f"/api/web/v1/accounts/{private_account_id}")
        assert direct.status_code == 404
        # B must not reach A's personal ledger.
        cross = await member_client.post(
            f"/api/web/v1/ledgers/{ids['owner_personal']}/select",
            headers=_csrf(member_csrf),
        )
        assert cross.status_code == 404


# ---------------------------------------------------------------------------
# WEB14–WEB15 — CSRF and session guards
# ---------------------------------------------------------------------------


async def test_web14_invalid_csrf_is_403(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf_token = await _client(factory, "ou_a")
    async with client:
        # No CSRF header at all.
        missing = await client.post(
            "/api/web/v1/entries",
            json=_entry_body(),
        )
        assert missing.status_code == 403
        # Wrong CSRF header.
        wrong = await client.post(
            "/api/web/v1/entries",
            headers=_csrf("not-the-real-token"),
            json=_entry_body(),
        )
        assert wrong.status_code == 403
        # Nothing was created by the CSRF-rejected attempts.
        current = await client.get("/api/web/v1/ledgers/current")
        assert await _entry_count(factory, uuid.UUID(current.json()["id"])) == 0


async def test_web15_invalid_session_is_401(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    auth = DashboardAuthService(settings(), factory)
    app = FastAPI()
    app.state.settings = settings()
    app.state.session_factory = factory
    app.include_router(router)
    app.dependency_overrides[_auth_service] = lambda: auth
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ledger.test"
    ) as client:
        me = await client.get("/api/web/v1/me")
        assert me.status_code == 401
        entries = await client.get("/api/web/v1/entries")
        assert entries.status_code == 401
        # A revoked session is also 401 (Journey E).
        created = await auth.create_session({"open_id": "ou_a", "name": "A", "avatar_url": ""})
        client.cookies.set(SESSION_COOKIE, created.session_token)
        await auth.revoke(created.session_token)
        revoked = await client.get("/api/web/v1/me")
        assert revoked.status_code == 401


async def test_web05b_transaction_detail_and_cross_ledger_404(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Entry detail shows the full contract; an entry in another user's ledger
    is a 404 (IDOR guard), never a leak."""
    owner_client, owner_csrf = await _client(factory, "ou_a")
    async with owner_client:
        created = await owner_client.post(
            "/api/web/v1/entries",
            headers={**_csrf(owner_csrf), "Idempotency-Key": "detail-create"},
            json=_entry_body(note="详情页"),
        )
        detail = await owner_client.get(
            f"/api/web/v1/entries/{created.json()['entry']['short_id']}"
        )
        assert detail.status_code == 200
        entry = detail.json()["entry"]
        for field in (
            "amount",
            "direction",
            "category",
            "note",
            "account_name",
            "occurred_at",
            "source_type",
            "created_at",
            "updated_at",
        ):
            assert field in entry, f"detail is missing {field}"
    outsider_client, _outsider_csrf = await _client(factory, "ou_outsider")
    async with outsider_client:
        guess = await outsider_client.get(
            f"/api/web/v1/entries/{created.json()['entry']['short_id']}"
        )
        assert guess.status_code == 404
