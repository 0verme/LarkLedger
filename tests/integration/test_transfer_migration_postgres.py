import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_transfer_migration_constraints_upgrade_and_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_transfer_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(command.upgrade, config, "20260809_0019")
        async with scratch_engine.begin() as connection:
            ledger_id = await connection.scalar(text("SELECT id FROM ledgers LIMIT 1"))
            if ledger_id is None:
                user_id = uuid.uuid4()
                ledger_id = uuid.uuid4()
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
            else:
                user_id = await connection.scalar(
                    text("SELECT owner_user_id FROM ledgers WHERE id = :id"),
                    {"id": ledger_id},
                )
                assert user_id is not None
            first = await connection.scalar(
                text("SELECT id FROM accounts WHERE ledger_id = :ledger LIMIT 1"),
                {"ledger": ledger_id},
            )
            if first is None:
                first = uuid.uuid4()
                await connection.execute(
                    text(
                        "INSERT INTO accounts (id, ledger_id, name, normalized_name, type, "
                        "currency, opening_balance, status, is_default) VALUES "
                        "(:id, :ledger, 'bank', 'bank', 'cash', 'CNY', 0, 'active', true)"
                    ),
                    {"id": first, "ledger": ledger_id},
                )
            second = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO accounts (id, ledger_id, name, normalized_name, type, "
                    "currency, opening_balance, status, is_default) VALUES "
                    "(:id, :ledger, 'wallet', 'wallet', 'asset', 'CNY', 0, 'active', false)"
                ),
                {"id": second, "ledger": ledger_id},
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        transfer_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO transfers (id, ledger_id, from_account_id, to_account_id, "
                    "actor_user_id, amount, currency, note, occurred_at, source_type) VALUES "
                    "(:id, :ledger, :source, :target, :actor, 10, 'CNY', '', now(), 'test')"
                ),
                {
                    "id": transfer_id,
                    "ledger": ledger_id,
                    "source": first,
                    "target": second,
                    "actor": user_id,
                },
            )
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO transfers (id, ledger_id, from_account_id, "
                        "to_account_id, actor_user_id, amount, currency, note, occurred_at, "
                        "source_type) VALUES (:id, :ledger, :account, :account, :actor, 1, "
                        "'CNY', '', now(), 'test')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "ledger": ledger_id,
                        "account": first,
                        "actor": user_id,
                    },
                )
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM transfers WHERE id = :id"), {"id": transfer_id}
            )
        await asyncio.to_thread(command.downgrade, config, "20260809_0019")
        async with scratch_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('public.transfers')")) is None
            assert await connection.scalar(text("SELECT count(*) FROM accounts")) >= 2
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'pending_commands' "
                        "AND column_name IN ('from_account_id', 'to_account_id', 'transfer_id')"
                    )
                )
                == 0
            )
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
