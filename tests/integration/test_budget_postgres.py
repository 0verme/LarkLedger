"""PostgreSQL integration tests for P28 period-scoped budgets.

Verifies the ``20260809_0022`` migration (upgrade creates the ``budgets`` table,
downgrade drops it), the database-level unique constraints for the ledger total
and per-category limits, the ledger foreign key, and service-level ledger /
period isolation and decimal accounting against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from lark_ledger.models import Budget, Direction, LedgerEntry

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_budget_migration_constraints_and_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_budget_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    maint_engine = create_async_engine(url.render_as_string(hide_password=False))
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))
        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        config = Config(str(_ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, config, "head")

        user_id = uuid.uuid4()
        ledger_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, display_name, status) VALUES (:id, '', 'active')"
                ),
                {"id": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledgers (id, owner_user_id, name, normalized_name, kind, "
                    "currency, timezone, is_default) VALUES (:id, :user, 'main', 'main', "
                    "'personal', 'CNY', 'Asia/Shanghai', true)"
                ),
                {"id": ledger_id, "user": user_id},
            )

        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                    "VALUES (:id, :ledger, '2026-08-01', NULL, 12000, 'CNY')"
                ),
                {"id": uuid.uuid4(), "ledger": ledger_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                    "VALUES (:id, :ledger, '2026-08-01', '餐饮', 3000, 'CNY')"
                ),
                {"id": uuid.uuid4(), "ledger": ledger_id},
            )
            # Distinct period rows coexist for the same category.
            await connection.execute(
                text(
                    "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                    "VALUES (:id, :ledger, '2026-07-01', '餐饮', 2000, 'CNY')"
                ),
                {"id": uuid.uuid4(), "ledger": ledger_id},
            )
            # Decimal amounts round-trip with scale 2.
            await connection.execute(
                text(
                    "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                    "VALUES (:id, :ledger, '2026-09-01', '购物', 1234.56, 'CNY')"
                ),
                {"id": uuid.uuid4(), "ledger": ledger_id},
            )
            amount = await connection.scalar(
                text(
                    "SELECT amount FROM budgets WHERE ledger_id = :ledger AND period = "
                    "'2026-09-01'"
                ),
                {"ledger": ledger_id},
            )
            assert amount == Decimal("1234.56")

        # Two ledger totals for the same period violate the partial unique index.
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                        "VALUES (:id, :ledger, '2026-08-01', NULL, 1, 'CNY')"
                    ),
                    {"id": uuid.uuid4(), "ledger": ledger_id},
                )

        # Two category limits for the same ledger+period+category violate the constraint.
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                        "VALUES (:id, :ledger, '2026-08-01', '餐饮', 1, 'CNY')"
                    ),
                    {"id": uuid.uuid4(), "ledger": ledger_id},
                )

        # A budget row must reference an existing ledger.
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO budgets (id, ledger_id, period, category, amount, currency) "
                        "VALUES (:id, :missing, '2026-08-01', NULL, 1, 'CNY')"
                    ),
                    {"id": uuid.uuid4(), "missing": uuid.uuid4()},
                )

        await asyncio.to_thread(command.downgrade, config, "20260809_0021")
        async with scratch_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('public.budgets')")) is None
            assert (
                await connection.scalar(text("SELECT count(*) FROM ledgers")) == 1
            )
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()


async def test_budget_service_ledger_and_period_isolation_on_postgres(
    postgres_session_factory,
) -> None:
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.client_application import ClientApplicationService
    from lark_ledger.services.identity import IdentityService

    async with postgres_session_factory() as session:
        context_a = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="test", external_subject_id="ou_pg_a")
        context_b = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="test", external_subject_id="ou_pg_b")
        app = ClientApplicationService(session, currency="CNY", timezone="Asia/Shanghai")

        await app.set_total_budget(context_a, period=date(2026, 8, 1), amount=Decimal("12000"))
        await app.set_category_budget(
            context_a, period=date(2026, 8, 1), category="餐饮", amount=Decimal("3000")
        )
        default_account = await AccountService(session).get_default(context_a)
        session.add(
            LedgerEntry(
                user_open_id="ou_pg_a",
                ledger_id=context_a.ledger_id,
                account_id=default_account.id,
                short_id="PG001",
                amount=Decimal("150"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
                source_type="test",
            )
        )
        await session.commit()

        august = await app.get_budget_overview(context_a, period=date(2026, 8, 1))
        assert august.total_limit_set is True
        assert august.total_budget == Decimal("12000")
        assert august.total_spent == Decimal("150")
        food = next(item for item in august.items if item.category == "餐饮")
        assert food.amount == Decimal("3000")
        assert food.spent == Decimal("150")

        # Ledger B sees nothing from ledger A.
        isolated = await app.get_budget_overview(context_b, period=date(2026, 8, 1))
        assert isolated.total_budget is None
        assert isolated.items == []

        # A different period on the same ledger stays empty.
        july = await app.get_budget_overview(context_a, period=date(2026, 7, 1))
        assert july.total_budget is None
        assert july.total_spent == Decimal("0")

        budgets = (await session.scalars(select(Budget))).all()
        assert len(budgets) == 2  # one total + one category, both ledger A
        assert all(row.ledger_id == context_a.ledger_id for row in budgets)
