import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_identity_ledger_migration_backfills_and_downgrades(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_identity_{uuid.uuid4().hex[:8]}"
    scratch_dsn = url.set(database=scratch).render_as_string(hide_password=False)
    base_dsn = url.render_as_string(hide_password=False)
    entry_id = uuid.uuid4()
    session_id = uuid.uuid4()

    maint_engine = create_async_engine(base_dsn)
    scratch_engine = create_async_engine(scratch_dsn)
    try:
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{scratch}"'))

        monkeypatch.setenv("LARK_LEDGER_DATABASE_URL", scratch_dsn)
        get_settings.cache_clear()
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "20260808_0014")

        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO ledger_entries "
                    "(id, user_open_id, short_id, amount, currency, direction, category, "
                    "note, occurred_at, source_type) VALUES "
                    "(:id, 'ou_migrate', 'A83F2', 28, 'CNY', 'EXPENSE', '餐饮', "
                    "'午饭', now(), 'text')"
                ),
                {"id": entry_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO dashboard_sessions "
                    "(id, token_hash, csrf_hash, user_open_id, display_name, avatar_url, "
                    "expires_at) VALUES "
                    "(:id, :token, :csrf, 'ou_migrate', '小飞', '', now() + interval '1 hour')"
                ),
                {"id": session_id, "token": "a" * 64, "csrf": "b" * 64},
            )

        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "head")
        async with scratch_engine.connect() as connection:
            user = (
                await connection.execute(
                    text("SELECT id, display_name, status FROM users")
                )
            ).one()
            identity = (
                await connection.execute(
                    text(
                        "SELECT user_id, channel, external_subject_id "
                        "FROM channel_identities"
                    )
                )
            ).one()
            ledger = (
                await connection.execute(
                    text("SELECT id, owner_user_id, kind, is_default FROM ledgers")
                )
            ).one()
            migrated_entry = (
                await connection.execute(
                    text("SELECT user_open_id, ledger_id FROM ledger_entries WHERE id = :id"),
                    {"id": entry_id},
                )
            ).one()
            migrated_session = (
                await connection.execute(
                    text(
                        "SELECT user_open_id, user_id, ledger_id "
                        "FROM dashboard_sessions WHERE id = :id"
                    ),
                    {"id": session_id},
                )
            ).one()

            assert user.display_name == "小飞"
            assert user.status == "active"
            assert identity == (user.id, "feishu", "ou_migrate")
            assert ledger.owner_user_id == user.id
            assert ledger.kind == "personal"
            assert ledger.is_default is True
            assert migrated_entry == ("ou_migrate", ledger.id)
            assert migrated_session == ("ou_migrate", user.id, ledger.id)

        await asyncio.to_thread(
            command.downgrade, Config(str(_ALEMBIC_INI)), "20260808_0014"
        )
        async with scratch_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('public.users')")) is None
            kept = await connection.scalar(
                text("SELECT user_open_id FROM ledger_entries WHERE id = :id"),
                {"id": entry_id},
            )
            assert kept == "ou_migrate"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
