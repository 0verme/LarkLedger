"""PostgreSQL coverage for Entries substring-search indexes."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.postgres


async def test_entries_search_indexes_use_trigram_operator_classes(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'ledger_entries' "
                    "AND indexname IN ("
                    "'ix_entries_note_trgm', 'ix_entries_category_trgm', "
                    "'ix_entries_short_id_trgm')"
                )
            )
        ).all()

    definitions = {row.indexname: row.indexdef for row in rows}
    assert set(definitions) == {
        "ix_entries_note_trgm",
        "ix_entries_category_trgm",
        "ix_entries_short_id_trgm",
    }
    assert all("USING gin" in definition for definition in definitions.values())
    assert all("gin_trgm_ops" in definition for definition in definitions.values())
