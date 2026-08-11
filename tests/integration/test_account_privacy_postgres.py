"""P32 account-privacy migration and Postgres behavior.

* Migration ``20260812_0025`` adds ``accounts.visibility`` (``shared`` default)
  and ``accounts.owner_user_id`` with CHECK constraints; existing rows backfill
  to shared/NULL; downgrade refuses while any private account exists.
* On real Postgres, the privacy service filters private accounts cross-ledger
  (a member never sees another member's private accounts / entries).
"""

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_account_privacy_migration_upgrade_and_guarded_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_privacy_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    base_dsn = url.render_as_string(hide_password=False)
    account_id = uuid.uuid4()
    ledger_id = uuid.uuid4()
    owner_id = uuid.uuid4()

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "head")

        async with scratch_engine.begin() as connection:
            # A personal ledger + owner so the FK targets exist.
            await connection.execute(
                text(
                    "INSERT INTO users (id, display_name, status) "
                    "VALUES (:id, 'A', 'active')"
                ),
                {"id": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledgers (id, owner_user_id, kind, is_default, "
                    "normalized_name, name, currency, timezone) "
                    "VALUES (:id, :owner, 'personal', true, 'a', 'A', 'CNY', 'Asia/Shanghai')"
                ),
                {"id": ledger_id, "owner": owner_id},
            )
            # Backfilled/shared default: visibility defaults to shared, no owner.
            await connection.execute(
                text(
                    "INSERT INTO accounts (id, ledger_id, name, normalized_name, type, "
                    "currency, opening_balance, status, is_default) "
                    "VALUES (:id, :ledger, '默认', '默认', 'cash', 'CNY', 0, 'active', true)"
                ),
                {"id": account_id, "ledger": ledger_id},
            )
            columns = (
                await connection.execute(
                    text(
                        "SELECT column_name, column_default FROM information_schema.columns "
                        "WHERE table_name = 'accounts' AND column_name IN "
                        "('visibility', 'owner_user_id') ORDER BY column_name"
                    )
                )
            ).all()
            assert [row[0] for row in columns] == ["owner_user_id", "visibility"]
            visibility = await connection.scalar(
                text("SELECT visibility FROM accounts WHERE id = :id"),
                {"id": account_id},
            )
            owner = await connection.scalar(
                text("SELECT owner_user_id FROM accounts WHERE id = :id"),
                {"id": account_id},
            )
            assert visibility == "shared"
            assert owner is None

        # CHECK: private accounts must have an owner (isolated with autocommit
        # so the failed statement cannot abort the setup transaction).
        async with scratch_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            try:
                await autocommit.execute(
                    text(
                        "UPDATE accounts SET visibility = 'private' WHERE id = :id"
                    ),
                    {"id": account_id},
                )
                raise AssertionError("private without owner must violate CHECK")
            except Exception:
                pass  # expected CHECK violation

        # Downgrade refuses while a private account exists.
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE accounts SET visibility = 'private', owner_user_id = :owner "
                    "WHERE id = :id"
                ),
                {"id": account_id, "owner": owner_id},
            )
        async with scratch_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM accounts WHERE visibility = 'private'")
                )
                == 1
            )
        try:
            await asyncio.to_thread(
                command.downgrade, Config(str(_ALEMBIC_INI)), "20260811_0024"
            )
            raise AssertionError("downgrade with private accounts must be refused")
        except RuntimeError:
            pass

        # After clearing private rows the downgrade proceeds and drops columns.
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE accounts SET visibility = 'shared', owner_user_id = NULL "
                    "WHERE id = :id"
                ),
                {"id": account_id},
            )
        await asyncio.to_thread(
            command.downgrade, Config(str(_ALEMBIC_INI)), "20260811_0024"
        )
        async with scratch_engine.connect() as connection:
            remaining = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'accounts' AND column_name IN "
                        "('visibility', 'owner_user_id')"
                    )
                )
            ).scalar_one()
            assert remaining == 0
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()


async def test_privacy_service_access_matrix_on_postgres(
    postgres_engine: AsyncEngine,
) -> None:
    """Cross-ledger: a member of one household never sees private accounts of
    another member, and a user outside the household sees nothing at all."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from lark_ledger.context import RequestContext
    from lark_ledger.models import (
        Account,
        AccountStatus,
        AccountType,
        AccountVisibility,
    )
    from lark_ledger.services.identity import IdentityService
    from lark_ledger.services.privacy import PrivacyService

    engine = postgres_engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
            owner = await IdentityService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).resolve_or_bootstrap(
                channel="feishu", external_subject_id="ou_pz_owner", display_name="A"
            )
            member = await IdentityService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).resolve_or_bootstrap(
                channel="feishu", external_subject_id="ou_pz_member", display_name="B"
            )
            outsider = await IdentityService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).resolve_or_bootstrap(
                channel="feishu", external_subject_id="ou_pz_out", display_name="C"
            )
            from lark_ledger.services.household_management import (
                HouseholdManagementService,
            )

            manager = HouseholdManagementService(
                session, currency="CNY", timezone="Asia/Shanghai"
            )
            home = await manager.create(owner.actor_user_id, "隐私家庭")
            invitation = await manager.invite(
                owner.actor_user_id, home.household.id, "ou_pz_member"
            )
            await manager.accept(member.actor_user_id, invitation.public_id)
            owner_ctx = RequestContext(
                actor_user_id=owner.actor_user_id,
                ledger_id=home.ledger.id,
                source_channel="feishu",
                external_subject_id="ou_pz_owner",
            )
            member_ctx = RequestContext(
                actor_user_id=member.actor_user_id,
                ledger_id=home.ledger.id,
                source_channel="feishu",
                external_subject_id="ou_pz_member",
            )
            outsider_ctx = RequestContext(
                actor_user_id=outsider.actor_user_id,
                ledger_id=home.ledger.id,
                source_channel="feishu",
                external_subject_id="ou_pz_out",
            )
            private_account = Account(
                ledger_id=home.ledger.id,
                name="私房钱",
                normalized_name="私房钱",
                type=AccountType.CASH.value,
                currency="CNY",
                opening_balance=0,
                status=AccountStatus.ACTIVE.value,
                is_default=False,
                visibility=AccountVisibility.PRIVATE.value,
                owner_user_id=owner.actor_user_id,
            )
            session.add(private_account)
            await session.commit()

            privacy = PrivacyService(session)
            assert await privacy.privacy_enabled(member_ctx) is True
            # Owner sees the private account; the member and outsider do not.
            assert await privacy.can_view_account(owner_ctx, private_account.id) is True
            assert await privacy.can_view_account(member_ctx, private_account.id) is False
            assert await privacy.can_view_account(outsider_ctx, private_account.id) is False
            member_visible = await privacy.visible_account_ids(member_ctx)
            assert private_account.id not in member_visible
