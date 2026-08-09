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


async def test_household_migration_constraints_and_lossless_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_household_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "20260809_0016")

        owner_id = uuid.uuid4()
        member_id = uuid.uuid4()
        personal_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, display_name, status) VALUES "
                    "(:owner, 'owner', 'active'), (:member, 'member', 'active')"
                ),
                {"owner": owner_id, "member": member_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledgers "
                    "(id, owner_user_id, name, normalized_name, kind, currency, "
                    "timezone, is_default) "
                    "VALUES (:id, :owner, '我的账本', '我的账本', 'personal', 'CNY', "
                    "'Asia/Shanghai', true)"
                ),
                {"id": personal_id, "owner": owner_id},
            )

        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "head")
        household_id = uuid.uuid4()
        shared_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO households (id, owner_user_id, name, normalized_name, status) "
                    "VALUES (:id, :owner, '小家', '小家', 'active')"
                ),
                {"id": household_id, "owner": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO household_members "
                    "(id, household_id, user_id, role, status, joined_at) "
                    "VALUES (:id, :household, :owner, 'owner', 'active', now())"
                ),
                {"id": uuid.uuid4(), "household": household_id, "owner": owner_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledgers "
                    "(id, owner_user_id, household_id, name, normalized_name, kind, currency, "
                    "timezone, is_default) VALUES (:id, NULL, :household, '小家公共账本', "
                    "'小家公共账本', 'household_shared', 'CNY', 'Asia/Shanghai', false)"
                ),
                {"id": shared_id, "household": household_id},
            )
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO household_members "
                        "(id, household_id, user_id, role, status, joined_at) "
                        "VALUES (:id, :household, :member, 'owner', 'active', now())"
                    ),
                    {"id": uuid.uuid4(), "household": household_id, "member": member_id},
                )
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO ledgers "
                        "(id, owner_user_id, household_id, name, normalized_name, kind, currency, "
                        "timezone, is_default) VALUES (:id, NULL, :household, '重复', '重复', "
                        "'household_shared', 'CNY', 'Asia/Shanghai', false)"
                    ),
                    {"id": uuid.uuid4(), "household": household_id},
                )

        async with scratch_engine.begin() as connection:
            await connection.execute(text("DELETE FROM ledgers WHERE id = :id"), {"id": shared_id})
            await connection.execute(
                text("DELETE FROM households WHERE id = :id"), {"id": household_id}
            )
        await asyncio.to_thread(
            command.downgrade, Config(str(_ALEMBIC_INI)), "20260809_0016"
        )
        async with scratch_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT owner_user_id FROM ledgers WHERE id = :id"),
                    {"id": personal_id},
                )
                == owner_id
            )
            assert await connection.scalar(text("SELECT to_regclass('public.households')")) is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
