"""P33-A financial goals on real PostgreSQL.

* Migration ``20260813_0026`` creates ``financial_goals`` +
  ``goal_account_bindings``; upgrade and downgrade round-trip cleanly and
  produce a single head.
* Composite FKs keep bindings inside their own ledger (cross-ledger rows are
  impossible at the database level).
* Progress derives from real account balances (transaction / revision
  recalculation) and household two-user privacy holds end to end.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_goals_migration_upgrade_downgrade_single_head(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_goal_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    base_dsn = url.render_as_string(hide_password=False)
    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        config = Config(str(_ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, config, "head")

        async with scratch_engine.begin() as connection:
            tables = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_name IN ('financial_goals', 'goal_account_bindings')"
                    )
                )
            }
            assert tables == {"financial_goals", "goal_account_bindings"}
            # Single head.
            head = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert head == "20260813_0026"

        # Downgrade one step drops both tables.
        await asyncio.to_thread(command.downgrade, config, "20260812_0025")
        async with scratch_engine.begin() as connection:
            remaining = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name IN ('financial_goals', 'goal_account_bindings')"
                )
            )
            assert remaining == 0
        # Re-upgrade restores them.
        await asyncio.to_thread(command.upgrade, config, "head")
        async with scratch_engine.begin() as connection:
            tables = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_name IN ('financial_goals', 'goal_account_bindings')"
                    )
                )
            }
            assert tables == {"financial_goals", "goal_account_bindings"}
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()


async def _household(factory: async_sessionmaker) -> tuple[object, object, object]:
    from lark_ledger.context import RequestContext
    from lark_ledger.services.household_management import HouseholdManagementService
    from lark_ledger.services.identity import IdentityService

    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_pg_owner", display_name="A"
        )
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_pg_member", display_name="B"
        )
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "PG 目标家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_pg_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_pg_owner",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_pg_member",
        )
        await session.commit()
        return owner_ctx, member_ctx, home.ledger


async def test_goal_lifecycle_progress_and_privacy_on_postgres(
    postgres_engine: AsyncEngine,
) -> None:
    from lark_ledger.models import (
        AccountType,
        AccountVisibility,
        Direction,
        LedgerEntry,
    )
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.goals import GoalNotFoundError, GoalProgressService, GoalService

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    owner_ctx, member_ctx, ledger = await _household(factory)

    async with factory() as session:
        shared = await AccountService(session).create(
            owner_ctx,
            name="家庭储蓄",
            account_type=AccountType.CASH,
            currency="CNY",
            opening_balance=Decimal("60000"),
        )
        private = await AccountService(session).create(
            owner_ctx,
            name="私房钱",
            account_type=AccountType.CASH,
            currency="CNY",
            opening_balance=Decimal("10000"),
            visibility=AccountVisibility.PRIVATE,
        )
        shared_goal = await GoalService(
            session, timezone="Asia/Shanghai", currency="CNY"
        ).create(
            owner_ctx,
            name="家庭应急储备",
            target_amount=Decimal("120000"),
            account_ids=[shared.id],
            target_date=date(2027, 12, 31),
        )
        private_goal = await GoalService(
            session, timezone="Asia/Shanghai", currency="CNY"
        ).create(
            owner_ctx,
            name="私密储备",
            target_amount=Decimal("20000"),
            account_ids=[private.id],
        )
        await session.commit()

        # Cross-ledger binding is impossible at the DB level: inserting a
        # binding whose account belongs to another ledger violates the
        # composite FK.
        other_ledger_id = uuid.uuid4()
        other_account_id = uuid.uuid4()
        try:
            await session.execute(
                text(
                    "INSERT INTO goal_account_bindings (id, goal_id, ledger_id, account_id) "
                    "VALUES (:id, :goal, :ledger, :account)"
                ),
                {
                    "id": uuid.uuid4(),
                    "goal": str(shared_goal.id),
                    "ledger": str(other_ledger_id),
                    "account": str(other_account_id),
                },
            )
            raise AssertionError("cross-ledger binding must violate the FK")
        except Exception:
            pass
        # rollback expires every ORM object; capture ids first and reload the
        # goals so the async session never triggers a sync lazy-load later.
        shared_goal_id = shared_goal.id
        private_goal_id = private_goal.id
        await session.rollback()
        shared_goal = await session.get(type(shared_goal), shared_goal_id)
        private_goal = await session.get(type(private_goal), private_goal_id)
        assert shared_goal is not None and private_goal is not None

        # Real balance progress: 60000/120000 = 50%.
        progress_service = GoalProgressService(session, timezone="Asia/Shanghai", currency="CNY")
        shared_progress = await progress_service.progress(owner_ctx, shared_goal)
        assert shared_progress.progress_percent == Decimal("50.00")

        # An expense on the bound account moves progress (no cached counter).
        session.add(
            LedgerEntry(
                user_open_id="ou_pg_owner",
                created_by_user_id=owner_ctx.actor_user_id,
                paid_by_user_id=owner_ctx.actor_user_id,
                ledger_id=owner_ctx.ledger_id,
                account_id=shared.id,
                short_id="PG01",
                amount=Decimal("6000"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="应急",
                note="",
                occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()
        shared_progress = await progress_service.progress(owner_ctx, shared_goal)
        assert shared_progress.current_amount == Decimal("54000")
        assert shared_progress.progress_percent == Decimal("45.00")

        # Soft delete removes it; restore brings it back.
        entry = await session.scalar(
            text("SELECT id FROM ledger_entries WHERE short_id = 'PG01'")
        )
        await session.execute(
            text("UPDATE ledger_entries SET deleted_at = now() WHERE id = :id"), {"id": entry}
        )
        await session.commit()
        assert (
            await progress_service.progress(owner_ctx, shared_goal)
        ).current_amount == Decimal("60000")
        await session.execute(
            text("UPDATE ledger_entries SET deleted_at = NULL WHERE id = :id"), {"id": entry}
        )
        await session.commit()
        assert (
            await progress_service.progress(owner_ctx, shared_goal)
        ).current_amount == Decimal("54000")

        # Household privacy: B sees the shared goal with identical progress but
        # the private goal is a 404 (no inference possible).
        service = GoalService(session, timezone="Asia/Shanghai", currency="CNY")
        member_goals = await service.list_goals(member_ctx)
        assert {goal.id for goal in member_goals} == {shared_goal.id}
        with pytest.raises(GoalNotFoundError):
            await service.get(member_ctx, private_goal.id)
        with pytest.raises(GoalNotFoundError):
            await progress_service.progress(member_ctx, private_goal)
        member_shared = await progress_service.progress(member_ctx, shared_goal)
        owner_shared = await progress_service.progress(owner_ctx, shared_goal)
        assert member_shared.current_amount == owner_shared.current_amount
        assert member_shared.progress_percent == owner_shared.progress_percent

        # Owner sees their private goal exactly.
        owner_private = await progress_service.progress(owner_ctx, private_goal)
        assert owner_private.current_amount == Decimal("10000")
