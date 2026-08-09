import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture
def postgres_url() -> str:
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        # Local fallback: read just TEST_POSTGRES_URL from the project .env
        # instead of exporting it, so Settings(_env_file=None) defaults in unit
        # tests are never polluted by other LARK_LEDGER_* variables.
        url = dotenv_values(Path(__file__).resolve().parents[2] / ".env").get(
            "TEST_POSTGRES_URL"
        )
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    return url


@pytest_asyncio.fixture
async def postgres_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE dashboard_sessions, event_replay_audits, "
                "budget_alerts, category_budgets, "
                "ledger_entry_revisions, ledger_entries, pending_commands, reply_outbox, "
                "processed_events, channel_identities, ledgers, users CASCADE"
            )
        )
    yield engine
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE dashboard_sessions, event_replay_audits, "
                "budget_alerts, category_budgets, "
                "ledger_entry_revisions, ledger_entries, pending_commands, reply_outbox, "
                "processed_events, channel_identities, ledgers, users CASCADE"
            )
        )
    await engine.dispose()


@pytest.fixture
def postgres_session_factory(
    postgres_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(postgres_engine, expire_on_commit=False)
