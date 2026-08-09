import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_account_migration_backfills_entries_and_downgrades_losslessly(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_account_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(command.upgrade, config, "20260809_0018")
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
                        "INSERT INTO ledgers (id, owner_user_id, name, normalized_name, "
                        "kind, currency, timezone, is_default) VALUES "
                        "(:id, :user, '我的账本', '我的账本', 'personal', 'CNY', "
                        "'Asia/Shanghai', true)"
                    ),
                    {"id": ledger_id, "user": user_id},
                )
            entry_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO ledger_entries (id, user_open_id, ledger_id, short_id, "
                    "amount, currency, direction, category, note, occurred_at, source_type) "
                    "VALUES (:id, 'ou_old', :ledger, 'A0001', 10, 'CNY', 'EXPENSE', "
                    "'餐饮', '', now(), 'text')"
                ),
                {"id": entry_id, "ledger": ledger_id},
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        async with scratch_engine.connect() as connection:
            account_id = await connection.scalar(
                text("SELECT account_id FROM ledger_entries WHERE id = :id"), {"id": entry_id}
            )
            assert account_id is not None
            assert (
                await connection.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'ledger_entries' AND column_name = 'account_id'"
                    )
                )
                == "NO"
            )
            assert (
                await connection.scalar(
                    text("SELECT is_default FROM accounts WHERE id = :id"), {"id": account_id}
                )
                is True
            )
        await asyncio.to_thread(command.downgrade, config, "20260809_0018")
        async with scratch_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT amount FROM ledger_entries WHERE id = :id"), {"id": entry_id}
                )
                == 10
            )
            assert await connection.scalar(text("SELECT to_regclass('public.accounts')")) is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
