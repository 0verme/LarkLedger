"""P29 PostgreSQL integration tests.

Verifies the ``20260810_0023`` migration (upgrade creates ``recurring_rules``
and ``recurring_occurrences`` and extends ``pending_commands``, downgrade drops
them), the database-level ``(rule_id, occurrence_date)`` unique constraint, the
ledger foreign keys, real ``FOR UPDATE SKIP LOCKED`` concurrency (two workers
claiming the same due rule still produce one occurrence + one pending), and
confirm idempotency (one transaction even under duplicate confirmation).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lark_ledger.models import (
    Direction,
    LedgerEntry,
    PendingCommand,
    RecurringFrequency,
    RecurringOccurrence,
    RecurringRule,
)

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
T0 = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)


async def test_recurring_migration_constraints_and_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_recur_{uuid.uuid4().hex[:8]}"
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
        account_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO users (id, display_name, status) VALUES (:id, '', 'active')"),
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
            await connection.execute(
                text(
                    "INSERT INTO accounts (id, ledger_id, name, normalized_name, type, "
                    "currency, opening_balance, status, is_default) VALUES "
                    "(:id, :ledger, '默认账户', '默认账户', 'cash', 'CNY', 0, 'active', true)"
                ),
                {"id": account_id, "ledger": ledger_id},
            )

        async with scratch_engine.begin() as connection:
            rule_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO recurring_rules (id, ledger_id, creator_user_id, "
                    "paid_by_user_id, account_id, transaction_type, amount, currency, "
                    "category, description, frequency, interval, next_occurrence, "
                    "anchor_day, status) VALUES "
                    "(:id, :ledger, :user, :user, :account, 'EXPENSE', 3500, 'CNY', '房租', "
                    "'房租', 'monthly', 1, '2026-09-08', 8, 'active')"
                ),
                {"id": rule_id, "ledger": ledger_id, "user": user_id, "account": account_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO recurring_occurrences (id, ledger_id, rule_id, "
                    "occurrence_date, status) VALUES (:id, :ledger, :rule, "
                    "'2026-09-08', 'pending')"
                ),
                {"id": uuid.uuid4(), "ledger": ledger_id, "rule": rule_id},
            )

        # Duplicate (rule_id, occurrence_date) violates the idempotency constraint.
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO recurring_occurrences (id, ledger_id, rule_id, "
                        "occurrence_date, status) VALUES (:id, :ledger, :rule, "
                        "'2026-09-08', 'pending')"
                    ),
                    {"id": uuid.uuid4(), "ledger": ledger_id, "rule": rule_id},
                )

        # Rule must reference an existing ledger and ledger-scoped account.
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO recurring_rules (id, ledger_id, creator_user_id, "
                        "paid_by_user_id, account_id, transaction_type, amount, currency, "
                        "category, description, frequency, interval, next_occurrence, "
                        "anchor_day, status) VALUES "
                        "(:id, :missing, :user, :user, :account, 'EXPENSE', 100, 'CNY', 'x', "
                        "'x', 'monthly', 1, '2026-09-08', 8, 'active')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "missing": uuid.uuid4(),
                        "user": user_id,
                        "account": account_id,
                    },
                )

        # pending_commands recurring linkage columns exist.
        async with scratch_engine.connect() as connection:
            column = await connection.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_commands' AND column_name = 'recurring_rule_id'"
                )
            )
            assert column == "recurring_rule_id"

        await asyncio.to_thread(command.downgrade, config, "20260809_0022")
        async with scratch_engine.connect() as connection:
            rules_table = await connection.scalar(
                text("SELECT to_regclass('public.recurring_rules')")
            )
            assert rules_table is None
            occurrences_table = await connection.scalar(
                text("SELECT to_regclass('public.recurring_occurrences')")
            )
            assert occurrences_table is None
            pending_rule_column = await connection.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pending_commands' AND column_name = 'recurring_rule_id'"
                )
            )
            assert pending_rule_column is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()


async def _seed_rule(
    postgres_session_factory: async_sessionmaker,
    subject: str,
) -> tuple[RecurringRule, str]:
    """Create a due rule for ``subject`` and return it + the subject open_id."""
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.identity import IdentityService
    from lark_ledger.services.recurring import RecurringService

    async with postgres_session_factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)
        await session.commit()
        account = await AccountService(session).get_default(context)
        rule = await RecurringService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).create(
            context,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("3500"),
            currency=None,
            category="房租",
            description="房租",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=date(2026, 8, 9),
            account_id=account.id,
            now=T0,
        )
        await session.commit()
        return rule, subject


async def test_worker_concurrency_produces_single_pending_on_postgres(
    postgres_session_factory: async_sessionmaker,
) -> None:
    from lark_ledger.config import Settings
    from lark_ledger.services.recurring_worker import RecurringWorkerStore

    rule, subject = await _seed_rule(postgres_session_factory, "ou_pg_recur")
    settings = Settings(_env_file=None, timezone="Asia/Shanghai")
    store = RecurringWorkerStore(postgres_session_factory, settings)

    # Two concurrent workers sweep the same due rule. The row lock + unique
    # occurrence constraint must yield exactly one occurrence and one pending.
    results = await asyncio.gather(
        store.claim_and_generate("worker-a", T0, 10),
        store.claim_and_generate("worker-b", T0 + timedelta(milliseconds=50), 10),
    )
    generated_total = sum(len(item[0]) for item in results)
    assert generated_total == 1

    async with postgres_session_factory() as session:
        pending_count = (
            await session.execute(select(func.count()).select_from(PendingCommand))
        ).scalar_one()
        occurrence_count = (
            await session.execute(select(func.count()).select_from(RecurringOccurrence))
        ).scalar_one()
        refreshed = await session.scalar(
            select(RecurringRule).where(RecurringRule.id == rule.id)
        )
        assert pending_count == 1
        assert occurrence_count == 1
        assert refreshed is not None
        assert refreshed.next_occurrence > date(2026, 8, 9)
    del subject


async def test_confirm_idempotency_on_postgres(
    postgres_session_factory: async_sessionmaker,
) -> None:
    from lark_ledger.config import Settings
    from lark_ledger.services.pending import PendingCommandStore
    from lark_ledger.services.recurring_worker import RecurringWorkerStore

    await _seed_rule(postgres_session_factory, "ou_pg_recur_confirm")
    settings = Settings(_env_file=None, timezone="Asia/Shanghai")
    store = RecurringWorkerStore(postgres_session_factory, settings)
    generated, _ = await store.claim_and_generate("worker-1", T0, 10)
    assert len(generated) == 1
    code = generated[0].confirmation_code

    pending_store = PendingCommandStore(postgres_session_factory, settings)
    now = T0 + timedelta(hours=1)
    first, _ = await pending_store.confirm_and_execute(
        user_open_id="ou_pg_recur_confirm",
        confirmation_code=code,
        reply_to_message_id="om_c1",
        confirm_event_id=None,
        exchange_rates=None,
        now=now,
    )
    second, _ = await pending_store.confirm_and_execute(
        user_open_id="ou_pg_recur_confirm",
        confirmation_code=code,
        reply_to_message_id="om_c2",
        confirm_event_id=None,
        exchange_rates=None,
        now=now + timedelta(seconds=1),
    )

    async with postgres_session_factory() as session:
        entry_count = (
            await session.execute(select(func.count()).select_from(LedgerEntry))
        ).scalar_one()
        occurrence = (await session.scalars(select(RecurringOccurrence))).one()
        entry = (await session.scalars(select(LedgerEntry))).one()
        assert entry_count == 1
        assert occurrence.status == "confirmed"
        assert occurrence.entry_id == entry.id
    assert "已记录" in first
    assert "已确认并已入账" in second


async def test_recurring_ledger_isolation_and_budget_on_postgres(
    postgres_session_factory: async_sessionmaker,
) -> None:
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.client_application import ClientApplicationService
    from lark_ledger.services.identity import IdentityService
    from lark_ledger.services.recurring import (
        RecurringRuleNotFoundError,
        RecurringRuleValidationError,
        RecurringService,
    )

    async with postgres_session_factory() as session:
        context_a = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_pg_iso_a")
        context_b = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_pg_iso_b")
        await session.commit()
        app = ClientApplicationService(session, currency="CNY", timezone="Asia/Shanghai")
        service = RecurringService(session, currency="CNY", timezone="Asia/Shanghai")

        account_a = await AccountService(session).get_default(context_a)
        rule = await service.create(
            context_a,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("3500"),
            currency=None,
            category="房租",
            description="房租",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=date(2026, 8, 9),
            account_id=account_a.id,
            now=T0,
        )

        # Ledger B cannot see or access ledger A's rule.
        assert await service.list(context_b) == []
        with pytest.raises(RecurringRuleNotFoundError):
            await service.get(context_b, rule.id)

        # A cross-ledger account is rejected.
        account_b = await AccountService(session).get_default(context_b)
        with pytest.raises(RecurringRuleValidationError):
            await service.create(
                context_a,
                transaction_type=Direction.EXPENSE,
                amount=Decimal("100"),
                currency=None,
                category="跨账本",
                description="",
                frequency=RecurringFrequency.MONTHLY,
                interval=1,
                next_occurrence=date(2026, 9, 1),
                account_id=account_b.id,
                now=T0,
            )

        # The rule does not touch ledger A's budget.
        await app.set_total_budget(context_a, period=date(2026, 8, 1), amount=Decimal("10000"))
        overview = await app.get_budget_overview(context_a, period=date(2026, 8, 1))
        assert overview.total_spent == Decimal("0")
