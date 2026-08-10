"""P30 household contribution — migration backfill + shared-ledger payer flows.

Covers the migration 0024 upgrade/downgrade and the multi-user semantics that
SQLite cannot express (two users, one shared ledger, payer != creator, and a
household member confirming another member's recurring pending).
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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lark_ledger.config import get_settings
from lark_ledger.context import RequestContext
from lark_ledger.models import Direction, LedgerEntry, RecurringFrequency
from lark_ledger.schemas import Action, ParsedCommand

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def _bootstrap(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    from lark_ledger.services.identity import IdentityService

    return await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=open_id, display_name=name)


async def _household(session: AsyncSession) -> tuple[RequestContext, RequestContext, object]:
    from lark_ledger.services.household_management import HouseholdManagementService

    owner = await _bootstrap(session, "ou_owner", "A")
    member = await _bootstrap(session, "ou_member", "B")
    manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
    home = await manager.create(owner.actor_user_id, "测试家庭")
    invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_member")
    await manager.accept(member.actor_user_id, invitation.public_id)
    await session.commit()
    owner_ctx = RequestContext(
        actor_user_id=owner.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_owner",
    )
    member_ctx = RequestContext(
        actor_user_id=member.actor_user_id,
        ledger_id=home.ledger.id,
        source_channel="feishu",
        external_subject_id="ou_member",
    )
    return owner_ctx, member_ctx, home


async def _record(
    session: AsyncSession, context: RequestContext, *, amount: str, payer: str | None = None
) -> LedgerEntry:
    from lark_ledger.services.ledger import LedgerService

    result = await LedgerService(session, commit_changes=False).execute(
        context,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal(amount),
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
            payer_reference=payer,
        ),
    )
    await session.commit()
    assert result.entry_id is not None
    entry = await session.get(LedgerEntry, result.entry_id)
    assert entry is not None
    return entry


async def test_household_contribution_migration_backfill_and_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_hh_contrib_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "20260810_0023")

        owner_id = uuid.uuid4()
        ledger_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        account_id = uuid.uuid4()
        rule_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, display_name, status) VALUES "
                    "(:id, 'A', 'active')"
                ),
                {"id": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledgers (id, owner_user_id, name, normalized_name, kind, "
                    "currency, timezone, is_default) VALUES "
                    "(:id, :owner, '我的账本', '我的账本', 'personal', 'CNY', "
                    "'Asia/Shanghai', true)"
                ),
                {"id": ledger_id, "owner": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO channel_identities (id, user_id, channel, "
                    "external_subject_id, current_ledger_id) VALUES "
                    "(:id, :user, 'feishu', 'ou_legacy', :ledger)"
                ),
                {"id": uuid.uuid4(), "user": owner_id, "ledger": ledger_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO accounts (id, ledger_id, name, normalized_name, type, "
                    "currency, opening_balance, status, is_default) VALUES "
                    "(:id, :ledger, '默认账户', '默认账户', 'cash', 'CNY', 0, 'active', true)"
                ),
                {"id": account_id, "ledger": ledger_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledger_entries (id, user_open_id, ledger_id, account_id, "
                    "short_id, amount, currency, direction, category, note, occurred_at, "
                    "source_type) VALUES (:id, 'ou_legacy', :ledger, :account, 'A83F2', "
                    "32.00, 'CNY', 'EXPENSE', 'food', 'lunch', now(), 'text')"
                ),
                {"id": entry_id, "ledger": ledger_id, "account": account_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO recurring_rules (id, ledger_id, creator_user_id, "
                    "account_id, transaction_type, amount, currency, category, description, "
                    "frequency, interval, next_occurrence, anchor_day, status) VALUES "
                    "(:id, :ledger, :owner, :account, 'EXPENSE', 300, 'CNY', 'housing', "
                    "'rent', 'monthly', 1, '2026-09-01', 1, 'active')"
                ),
                {
                    "id": rule_id,
                    "ledger": ledger_id,
                    "owner": owner_id,
                    "account": account_id,
                },
            )

        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "head")
        async with scratch_engine.connect() as connection:
            row_result = await connection.execute(
                text(
                    "SELECT created_by_user_id, paid_by_user_id FROM ledger_entries "
                    "WHERE id = :id"
                ),
                {"id": entry_id},
            )
            row = row_result.first()
            assert row is not None
            assert row[0] == owner_id  # created_by backfilled
            assert row[1] == owner_id  # paid_by backfilled = creator
            rule_paid_by = await connection.scalar(
                text("SELECT paid_by_user_id FROM recurring_rules WHERE id = :id"),
                {"id": rule_id},
            )
            assert rule_paid_by == owner_id  # recurring paid_by backfilled to creator
            column_rows = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ledger_entries' AND column_name IN "
                    "('created_by_user_id', 'paid_by_user_id')"
                )
            )
            columns = {item[0] for item in column_rows}
            assert columns == {"created_by_user_id", "paid_by_user_id"}

        await asyncio.to_thread(command.downgrade, Config(str(_ALEMBIC_INI)), "20260810_0023")
        async with scratch_engine.connect() as connection:
            column_rows = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ledger_entries' AND column_name IN "
                    "('created_by_user_id', 'paid_by_user_id')"
                )
            )
            columns = {item[0] for item in column_rows}
            assert columns == set()
            assert await connection.scalar(
                text("SELECT to_regclass('public.household_members')")
            ) is not None  # table predates 0024
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()


async def test_two_users_one_household_payer_and_stats(
    postgres_session_factory,
) -> None:
    async with postgres_session_factory() as session:
        owner_ctx, member_ctx, _ = await _household(session)

        a_entry = await _record(session, owner_ctx, amount="100")
        b_entry = await _record(session, member_ctx, amount="200")
        # created_by=A, paid_by=B for the reference-resolved expense.
        c_entry = await _record(session, owner_ctx, amount="50", payer="B")

        assert a_entry.paid_by_user_id == owner_ctx.actor_user_id
        assert b_entry.paid_by_user_id == member_ctx.actor_user_id
        assert c_entry.created_by_user_id == owner_ctx.actor_user_id
        assert c_entry.paid_by_user_id == member_ctx.actor_user_id

        from lark_ledger.services.member_stats import MemberStatsService

        stats = await MemberStatsService(session).stats(owner_ctx)
        by_user = {item.user_id: item for item in stats}
        assert by_user[str(owner_ctx.actor_user_id)].expense_total == Decimal("100")
        assert by_user[str(member_ctx.actor_user_id)].expense_total == Decimal("250")

        from lark_ledger.services.budget import BudgetService

        overview = await BudgetService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).overview(owner_ctx, period=date(2026, 8, 1))
        assert overview.total_spent == Decimal("350")


async def test_concurrent_member_writes_same_ledger(
    postgres_session_factory,
) -> None:
    """Two members write to the same shared ledger without cross-user pollution."""
    async with postgres_session_factory() as session:
        owner_ctx, member_ctx, _ = await _household(session)
        owner_id, member_id = owner_ctx.actor_user_id, member_ctx.actor_user_id
        ledger_id = owner_ctx.ledger_id

    async def write(ctx: RequestContext, amount: str) -> uuid.UUID:
        async with postgres_session_factory() as session:
            entry = await _record(session, ctx, amount=amount)
            return entry.id

    results = await asyncio.gather(
        write(owner_ctx, "10"),
        write(member_ctx, "20"),
        write(owner_ctx, "30"),
        write(member_ctx, "40"),
    )
    assert len(results) == 4
    async with postgres_session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(LedgerEntry).where(LedgerEntry.ledger_id == ledger_id)
                )
            ).all()
        )
        assert len(rows) == 4
        assert len({row.short_id for row in rows}) == 4  # unique short ids
        assert {row.paid_by_user_id for row in rows} == {owner_id, member_id}


async def test_recurring_cross_member_confirm_keeps_payer(
    postgres_session_factory,
) -> None:
    """Case G on Postgres: A confirms a shared recurring pending, paid_by stays B."""
    from lark_ledger.config import Settings
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.pending import PendingCommandStore
    from lark_ledger.services.recurring import RecurringService

    async with postgres_session_factory() as session:
        owner_ctx, member_ctx, _ = await _household(session)
        account = (await AccountService(session).list(owner_ctx))[0]
        rule = await RecurringService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).create(
            owner_ctx,
            transaction_type=Direction.EXPENSE,
            amount=Decimal("300"),
            currency=None,
            category="居住",
            description="房租",
            frequency=RecurringFrequency.MONTHLY,
            interval=1,
            next_occurrence=date(2026, 9, 1),
            account_id=account.id,
            paid_by_user_id=member_ctx.actor_user_id,
        )
        assert rule.paid_by_user_id == member_ctx.actor_user_id
        await session.commit()

        settings = Settings(
            _env_file=None,
            lark_app_id="cli_test",
            lark_app_secret="app-secret",
            pending_expires_seconds=3600,
            currency="CNY",
            timezone="Asia/Shanghai",
        )
        store = PendingCommandStore(postgres_session_factory, settings)
        pending = await store.create_recurring_pending(
            session=session,
            context=owner_ctx,
            user_open_id="ou_owner",
            rule=rule,
            occurrence_date=date(2026, 9, 1),
            now=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        )
        code = pending.confirmation_code
        await session.commit()

        # Member B confirms a pending created by A.
        found = await store.get_by_code("ou_member", code)
        assert found is not None and found.id == pending.id
        message, _ = await store.confirm_and_execute(
            user_open_id="ou_member",
            confirmation_code=code,
            reply_to_message_id="om_confirm",
            confirm_event_id=None,
            exchange_rates=None,
            now=datetime(2026, 9, 1, 0, 30, tzinfo=UTC),
        )
        assert "已记录" in message
        entry = (
            await session.execute(
                select(LedgerEntry).where(LedgerEntry.ledger_id == owner_ctx.ledger_id)
            )
        ).scalars().all()[-1]
        assert entry.created_by_user_id == member_ctx.actor_user_id
        assert entry.paid_by_user_id == member_ctx.actor_user_id
