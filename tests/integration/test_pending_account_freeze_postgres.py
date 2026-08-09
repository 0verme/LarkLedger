import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def test_pending_account_freeze_migration_upgrade_downgrade_and_legacy_compat(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic.config import Config

    from alembic import command
    from lark_ledger.config import get_settings

    url = make_url(postgres_url)
    scratch = f"lark_ledger_mig_pending_acc_{uuid.uuid4().hex[:8]}"
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
        await asyncio.to_thread(command.upgrade, config, "20260809_0020")

        # Seed a ledger with two accounts and legacy pending rows that predate
        # the account freeze column (a transfer pending and a plain create
        # pending with no account target column at all).
        async with scratch_engine.begin() as connection:
            ledger_id = await connection.scalar(text("SELECT id FROM ledgers LIMIT 1"))
            if ledger_id is None:
                user_id = uuid.uuid4()
                ledger_id = uuid.uuid4()
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
            legacy_create_code = "C1A001"
            legacy_transfer_code = "C1A002"
            expires = datetime.now(UTC) + timedelta(hours=1)
            for code, command_type in (
                (legacy_create_code, "create"),
                (legacy_transfer_code, "transfer"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO pending_commands (id, confirmation_code, user_open_id, "
                        "ledger_id, source_event_id, transport, source_type, command_type, "
                        "payload_version, payload_json, preview_json, risk_reason, status, "
                        "expires_at, from_account_id, to_account_id, transfer_id) VALUES "
                        "(:id, :code, 'ou_legacy', :ledger, :event, 'feishu', 'text', :cmd, 1, "
                        "'{}', '{}', 'duplicate', 'pending', :expires, :frm, :to, :tf)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "code": code,
                        "ledger": ledger_id,
                        "event": f"evt-{code}",
                        "cmd": command_type,
                        "expires": expires,
                        "frm": None if command_type == "create" else first,
                        "to": None if command_type == "create" else second,
                        "tf": None if command_type == "create" else uuid.uuid4(),
                    },
                )

        # Upgrade to head (0021): account_id column added, legacy rows stay NULL.
        await asyncio.to_thread(command.upgrade, config, "head")
        async with scratch_engine.connect() as connection:
            create_account_id = await connection.scalar(
                text(
                    "SELECT account_id FROM pending_commands WHERE confirmation_code = :code"
                ),
                {"code": legacy_create_code},
            )
            assert create_account_id is None
            transfer_account_id = await connection.scalar(
                text(
                    "SELECT account_id FROM pending_commands WHERE confirmation_code = :code"
                ),
                {"code": legacy_transfer_code},
            )
            assert transfer_account_id is None

            # A newly frozen single-account pending is valid and account-scoped.
            new_code = "C1A003"
            new_pending_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO pending_commands (id, confirmation_code, user_open_id, "
                    "ledger_id, account_id, source_event_id, transport, source_type, "
                    "command_type, payload_version, payload_json, preview_json, "
                    "risk_reason, status, expires_at) VALUES (:id, :code, 'ou_new', "
                    ":ledger, :account, 'evt-new', 'feishu', 'text', 'create', 1, '{}', "
                    "'{}', 'duplicate', 'pending', :expires)"
                ),
                {
                    "id": new_pending_id,
                    "code": new_code,
                    "ledger": ledger_id,
                    "account": first,
                    "expires": expires,
                },
            )
            await connection.commit()
            # A row mixing an account target with a transfer target is invalid.
            with pytest.raises(IntegrityError):
                async with scratch_engine.begin() as connection2:
                    await connection2.execute(
                        text(
                            "INSERT INTO pending_commands (confirmation_code, user_open_id, "
                            "ledger_id, account_id, from_account_id, to_account_id, "
                            "transfer_id, source_event_id, transport, source_type, "
                            "command_type, payload_version, payload_json, preview_json, "
                            "risk_reason, status, expires_at) VALUES ('C1A004', 'ou_new', "
                            ":ledger, :account, :account, :account2, :tf, 'evt-bad', "
                            "'feishu', 'text', 'transfer', 1, '{}', '{}', 'transfer', "
                            "'pending', :expires)"
                        ),
                        {
                            "ledger": ledger_id,
                            "account": first,
                            "account2": second,
                            "tf": uuid.uuid4(),
                            "expires": expires,
                        },
                    )
            # The frozen account must belong to the same ledger.
            other_ledger = uuid.uuid4()
            with pytest.raises(IntegrityError):
                async with scratch_engine.begin() as connection2:
                    await connection2.execute(
                        text(
                            "INSERT INTO pending_commands (confirmation_code, user_open_id, "
                            "ledger_id, account_id, source_event_id, transport, source_type, "
                            "command_type, payload_version, payload_json, preview_json, "
                            "risk_reason, status, expires_at) VALUES ('C1A005', 'ou_new', "
                            ":ledger, :account, 'evt-cross', 'feishu', 'text', 'create', 1, "
                            "'{}', '{}', 'duplicate', 'pending', :expires)"
                        ),
                        {
                            "ledger": other_ledger,
                            "account": first,
                            "expires": expires,
                        },
                    )

        # Downgrade to 0020: account_id column and its FK are removed; the old
        # transfer-only target constraint is restored and legacy rows survive.
        await asyncio.to_thread(command.downgrade, config, "20260809_0020")
        async with scratch_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'pending_commands' AND column_name = 'account_id'"
                    )
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM pending_commands WHERE confirmation_code IN "
                         "('C1A001', 'C1A002', 'C1A003')")
                )
                == 3
            )
            transfer_legacy = await connection.scalar(
                text(
                    "SELECT count(*) FROM pending_commands WHERE confirmation_code = 'C1A002' "
                    "AND from_account_id IS NOT NULL AND transfer_id IS NOT NULL"
                )
            )
            assert transfer_legacy == 1
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await scratch_engine.dispose()
        async with maint_engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        await maint_engine.dispose()
