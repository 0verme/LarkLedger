import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import (
    EventProcessStatus,
    parse_stored_payload,
)
from lark_ledger.models import Direction, LedgerEntry, ProcessedEvent
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.events import EventService
from lark_ledger.services.ledger import LedgerService

pytestmark = pytest.mark.postgres


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def process(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _message_event(message_id: str) -> dict[str, Any]:
    return {
        "sender": {"sender_id": {"open_id": "ou_integration"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": '{"text":"integration"}',
        },
    }


async def test_alembic_schema_is_at_head(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "20260805_0006"


async def test_list_keyset_and_get_entry_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    when = datetime(2026, 8, 5, 12, tzinfo=UTC)
    async with postgres_session_factory() as session:
        for index, code in enumerate(["PG001", "PG002", "PG003"]):
            entry = LedgerEntry(
                user_open_id="ou_pg",
                short_id=code,
                amount=Decimal(str(index + 1)),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=when,
                source_type="text",
            )
            session.add(entry)
            await session.flush()
            entry.created_at = when + timedelta(seconds=index)
            entry.updated_at = entry.created_at
        await session.commit()

        service = LedgerService(session)
        page1 = await service.execute(
            "ou_pg", ParsedCommand(action=Action.LIST_ENTRIES, limit=2)
        )
        # Newest by created_at: PG003, PG002
        assert "1. #PG003" in page1.message
        assert "2. #PG002" in page1.message
        assert "查看 #PG002 之前的2笔" in page1.message
        page2 = await service.execute(
            "ou_pg",
            ParsedCommand(
                action=Action.LIST_ENTRIES,
                limit=2,
                before_entry_ref="PG002",
            ),
        )
        assert "最近 1 笔账目" in page2.message
        assert "#PG001" in page2.message
        detail = await service.execute(
            "ou_pg", ParsedCommand(action=Action.GET_ENTRY, entry_ref="PG001")
        )
        assert "短 ID：#PG001" in detail.message
        assert "ou_pg" not in detail.message


async def test_entry_mutation_and_revision_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from lark_ledger.models import LedgerEntryRevision

    async with postgres_session_factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_mut",
                short_id="MT01A",
                amount=Decimal("10.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="x",
                occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()
        service = LedgerService(session)
        updated = await service.execute(
            "ou_mut",
            ParsedCommand(
                action=Action.UPDATE_ENTRY,
                entry_ref="MT01A",
                amount=Decimal("12.00"),
            ),
        )
        assert "已修改 #MT01A" in updated.message
        deleted = await service.execute(
            "ou_mut", ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="MT01A")
        )
        assert "已删除 #MT01A" in deleted.message
        restored = await service.execute(
            "ou_mut", ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref="MT01A")
        )
        assert "已恢复 #MT01A" in restored.message
        count = await session.scalar(select(func.count()).select_from(LedgerEntryRevision))
        assert count == 3
        entry = (
            await session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.user_open_id == "ou_mut",
                    LedgerEntry.short_id == "MT01A",
                )
            )
        ).scalar_one()
        assert entry.amount == Decimal("12.00")
        assert entry.deleted_at is None
        assert entry.short_id == "MT01A"


async def test_export_entries_query_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P04: export query, isolation, half-open range, deleted filter, stable sort."""
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 7, 1, tzinfo=UTC)
    mid = datetime(2026, 6, 15, 8, tzinfo=UTC)
    async with postgres_session_factory() as session:
        for index, (user, code, deleted, when) in enumerate(
            [
                ("ou_export_a", "EX001", False, mid),
                ("ou_export_a", "EX002", True, mid + timedelta(hours=1)),
                ("ou_export_a", "EX003", False, mid + timedelta(hours=2)),
                ("ou_export_b", "EX001", False, mid),
                ("ou_export_a", "EXAGE", False, datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        ):
            entry = LedgerEntry(
                user_open_id=user,
                short_id=code,
                amount=Decimal(str(index + 1)),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮" if user == "ou_export_a" else "他户",
                note="secret-b" if user == "ou_export_b" else "",
                occurred_at=when,
                source_type="text",
                deleted_at=when if deleted else None,
            )
            session.add(entry)
            await session.flush()
            entry.created_at = when + timedelta(seconds=index)
            entry.updated_at = entry.created_at
        await session.commit()

        service = LedgerService(session)
        default = await service.execute(
            "ou_export_a",
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=start,
                range_end=end,
            ),
        )
        assert default.export is not None
        body = default.export.content.decode("utf-8-sig")
        assert "#EX001" in body
        assert "#EX003" in body
        assert "#EX002" not in body
        assert "#EXAGE" not in body
        assert "他户" not in body
        assert "secret-b" not in body
        assert "ou_export" not in body
        rows = [line for line in body.splitlines() if line.startswith("#")]
        assert rows[0].startswith("#EX001")
        assert rows[1].startswith("#EX003")

        with_deleted = await service.execute(
            "ou_export_a",
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=start,
                range_end=end,
                include_deleted=True,
            ),
        )
        assert with_deleted.export is not None
        assert with_deleted.export.row_count == 3
        assert "#EX002" in with_deleted.export.content.decode("utf-8-sig")

        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "20260805_0006"


async def test_short_id_unique_per_user_allows_cross_user_reuse(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_a",
                short_id="A83F2",
                amount=Decimal("1.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()

        session.add(
            LedgerEntry(
                user_open_id="ou_a",
                short_id="A83F2",
                amount=Decimal("2.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="交通",
                note="",
                occurred_at=datetime(2026, 8, 5, 13, tzinfo=UTC),
                source_type="text",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(
            LedgerEntry(
                user_open_id="ou_b",
                short_id="A83F2",
                amount=Decimal("3.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="购物",
                note="",
                occurred_at=datetime(2026, 8, 5, 14, tzinfo=UTC),
                source_type="text",
            )
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(LedgerEntry))
        assert count == 2


async def test_concurrent_event_claim_is_processed_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    processor = RecordingProcessor()
    service = EventService(postgres_session_factory, processor)
    event = _message_event("om_concurrent")

    results = await asyncio.gather(
        *(service.handle("evt_concurrent", event, transport="webhook") for _ in range(8))
    )

    assert sum(results) == 1
    assert len(processor.events) == 1
    assert processor.events[0]["message"]["message_id"] == "om_concurrent"

    async with postgres_session_factory() as session:
        row = await session.get(ProcessedEvent, "evt_concurrent")
        assert row is not None
        assert row.payload_json is not None
        assert row.payload_version == 1
        assert row.transport == "webhook"
        assert row.status == EventProcessStatus.SUCCEEDED.value
        parsed = parse_stored_payload(row.payload_json)
        assert parsed["event"]["message"]["message_id"] == "om_concurrent"


def make_entry(source_item_index: int, short_id: str | None = None) -> LedgerEntry:
    # Distinct Crockford codes for integration fixtures (no I/L/O/U).
    defaults = ("AAAAA", "AAAAB", "AAAAC", "AAAAD", "AAAAE")
    return LedgerEntry(
        user_open_id="ou_integration",
        short_id=short_id or defaults[source_item_index],
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
        session.add(
            ProcessedEvent(
                event_id="evt_existing",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
            )
        )
        await session.commit()

        session.add(
            ProcessedEvent(
                event_id="evt_existing",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        session.add(
            ProcessedEvent(
                event_id="evt_after_rollback",
                status=EventProcessStatus.LEGACY_SUCCEEDED.value,
            )
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(ProcessedEvent))
        legacy = await session.get(ProcessedEvent, "evt_existing")
        assert legacy is not None
        assert legacy.payload_json is None
        assert legacy.status == EventProcessStatus.LEGACY_SUCCEEDED.value

    assert count == 2
