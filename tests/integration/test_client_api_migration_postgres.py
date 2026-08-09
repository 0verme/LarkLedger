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


async def test_client_api_migration_constraints_and_lossless_downgrade(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_client_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "20260809_0017")
        user_id = uuid.uuid4()
        ledger_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, display_name, status) "
                    "VALUES (:id, 'existing user', 'active')"
                ),
                {"id": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO ledgers "
                    "(id, owner_user_id, name, normalized_name, kind, currency, timezone, "
                    "is_default) VALUES (:id, :user, 'existing', 'existing', 'personal', "
                    "'CNY', 'Asia/Shanghai', true)"
                ),
                {"id": ledger_id, "user": user_id},
            )
        await asyncio.to_thread(command.upgrade, Config(str(_ALEMBIC_INI)), "head")
        credential_id = uuid.uuid4()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO client_credentials "
                    "(id, user_id, current_ledger_id, name, token_digest, token_prefix, scopes) "
                    "VALUES (:id, :user, :ledger, 'device', :digest, 'llv1_test', "
                    "'ledger:read')"
                ),
                {
                    "id": credential_id,
                    "user": user_id,
                    "ledger": ledger_id,
                    "digest": "a" * 64,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO client_idempotency_records "
                    "(id, actor_user_id, ledger_id, operation, idempotency_key, "
                    "request_digest, expires_at) VALUES "
                    "(:id, :user, :ledger, 'entry.create', 'key', :digest, now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "user": user_id,
                    "ledger": ledger_id,
                    "digest": "b" * 64,
                },
            )
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO client_credentials "
                        "(id, user_id, name, token_digest, token_prefix, scopes) "
                        "VALUES (:id, :user, 'duplicate', :digest, 'llv1_dup', 'ledger:read')"
                    ),
                    {"id": uuid.uuid4(), "user": user_id, "digest": "a" * 64},
                )
        with pytest.raises(IntegrityError):
            async with scratch_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO client_idempotency_records "
                        "(id, actor_user_id, ledger_id, operation, idempotency_key, "
                        "request_digest, expires_at) VALUES "
                        "(:id, :user, :ledger, 'entry.create', 'key', :digest, now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "user": user_id,
                        "ledger": ledger_id,
                        "digest": "c" * 64,
                    },
                )
        async with scratch_engine.begin() as connection:
            await connection.execute(text("DELETE FROM client_idempotency_records"))
            await connection.execute(text("DELETE FROM client_credentials"))
        await asyncio.to_thread(
            command.downgrade, Config(str(_ALEMBIC_INI)), "20260809_0017"
        )
        async with scratch_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT display_name FROM users WHERE id = :id"), {"id": user_id}
                )
                == "existing user"
            )
            assert (
                await connection.scalar(
                    text("SELECT to_regclass('public.client_credentials')")
                )
                is None
            )
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
