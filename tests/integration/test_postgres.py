import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.models import Direction, LedgerEntry, ProcessedEvent
from lark_ledger.services.events import EventService

pytestmark = pytest.mark.postgres


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def process(self, event: dict[str, Any]) -> None:
        self.events.append(event)


async def test_alembic_schema_is_at_head(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "20260803_0003"


async def test_concurrent_event_claim_is_processed_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    processor = RecordingProcessor()
    service = EventService(postgres_session_factory, processor)
    event = {"message": {"message_id": "om_concurrent"}}

    results = await asyncio.gather(
        *(service.handle("evt_concurrent", event) for _ in range(8))
    )

    assert sum(results) == 1
    assert processor.events == [event]


def make_entry(source_item_index: int) -> LedgerEntry:
    return LedgerEntry(
        user_open_id="ou_integration",
        amount=Decimal("12.34"),
        currency="CNY",
        direction=Direction.EXPENSE,
        category="餐饮",
        note="integration test",
        occurred_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        source_type="text",
        source_message_id="om_batch",
        source_item_index=source_item_index,
    )


async def test_batch_source_constraint_allows_distinct_items_only(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add_all([make_entry(0), make_entry(1)])
        await session.commit()
        session.add(make_entry(1))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        count = await session.scalar(select(func.count()).select_from(LedgerEntry))

    assert count == 2


async def test_transaction_can_continue_after_unique_violation(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add(ProcessedEvent(event_id="evt_existing"))
        await session.commit()

        session.add(ProcessedEvent(event_id="evt_existing"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(ProcessedEvent(event_id="evt_after_rollback"))
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(ProcessedEvent))

    assert count == 2
