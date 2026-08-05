import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
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
        pytest.skip("TEST_POSTGRES_URL is not configured")
    return url


@pytest_asyncio.fixture
async def postgres_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE budget_alerts, category_budgets, "
                "ledger_entry_revisions, ledger_entries, processed_events CASCADE"
            )
        )
    yield engine
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE budget_alerts, category_budgets, "
                "ledger_entry_revisions, ledger_entries, processed_events CASCADE"
            )
        )
    await engine.dispose()


@pytest.fixture
def postgres_session_factory(
    postgres_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(postgres_engine, expire_on_commit=False)
