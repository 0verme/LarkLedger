from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.entry_commands import (
    bind_entry_refs_from_message,
    try_parse_deterministic_entry_command,
)
from lark_ledger.entry_revisions import SNAPSHOT_VERSION, RevisionChangeType
from lark_ledger.models import Direction, LedgerEntry, LedgerEntryRevision
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ledger import LedgerService


async def _seed(
    session: AsyncSession,
    *,
    user: str = "ou_user",
    short_id: str = "A83F2",
    amount: str = "32.00",
    category: str = "餐饮",
    note: str = "午饭",
    deleted: bool = False,
) -> LedgerEntry:
    entry = LedgerEntry(
        user_open_id=user,
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=Direction.EXPENSE,
        category=category,
        note=note,
        occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        source_type="text",
        deleted_at=datetime(2026, 8, 5, 13, tzinfo=UTC) if deleted else None,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def test_update_entry_fields_and_revision(session: AsyncSession) -> None:
    entry = await _seed(session)
    service = LedgerService(session)
    result = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.UPDATE_ENTRY,
            entry_ref="#a83f2",
            amount=Decimal("35"),
            category="交通",
            note="地铁",
        ),
    )
    assert "已修改 #A83F2" in result.message
    assert "¥35.00" in result.message
    assert "交通" in result.message
    await session.refresh(entry)
    assert entry.amount == Decimal("35.00")
    assert entry.category == "交通"
    assert entry.note == "地铁"
    rev = (await session.execute(select(LedgerEntryRevision))).scalar_one()
    assert rev.change_type == RevisionChangeType.UPDATE.value
    assert rev.before_json["amount"] == "32.00"
    assert rev.after_json["amount"] == "35.00"
    assert rev.before_json["snapshot_version"] == SNAPSHOT_VERSION
    assert rev.user_open_id == "ou_user"
    assert rev.short_id == "A83F2"


async def test_update_noop_and_deleted_blocked(session: AsyncSession) -> None:
    entry = await _seed(session)
    service = LedgerService(session)
    noop = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.UPDATE_ENTRY,
            entry_ref="A83F2",
            amount=Decimal("32.00"),
            category="餐饮",
            note="午饭",
        ),
    )
    assert "没有变化" in noop.message
    assert (await session.scalar(select(func.count()).select_from(LedgerEntryRevision))) == 0

    entry.deleted_at = datetime.now(UTC)
    await session.commit()
    blocked = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.UPDATE_ENTRY, entry_ref="A83F2", amount=Decimal("10")),
    )
    assert "已删除，请先恢复" in blocked.message


async def test_clear_note_and_partial_update(session: AsyncSession) -> None:
    entry = await _seed(session)
    service = LedgerService(session)
    result = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.UPDATE_ENTRY, entry_ref="A83F2", clear_note=True),
    )
    assert "已修改" in result.message
    await session.refresh(entry)
    assert entry.note == ""
    assert entry.amount == Decimal("32.00")
    assert entry.category == "餐饮"


async def test_delete_restore_idempotent_and_list(session: AsyncSession) -> None:
    await _seed(session)
    service = LedgerService(session)
    deleted = await service.execute(
        "ou_user", ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="#A83F2")
    )
    assert "已删除 #A83F2" in deleted.message
    assert "恢复 #A83F2" in deleted.message
    listed = await service.execute("ou_user", ParsedCommand(action=Action.LIST_ENTRIES))
    assert "没有符合条件的账目" in listed.message
    detail = await service.execute(
        "ou_user", ParsedCommand(action=Action.GET_ENTRY, entry_ref="A83F2")
    )
    assert "状态：已删除" in detail.message

    again = await service.execute(
        "ou_user", ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="A83F2")
    )
    assert "已经处于删除状态" in again.message
    assert (await session.scalar(select(func.count()).select_from(LedgerEntryRevision))) == 1

    restored = await service.execute(
        "ou_user", ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref="A83F2")
    )
    assert "已恢复 #A83F2" in restored.message
    listed2 = await service.execute("ou_user", ParsedCommand(action=Action.LIST_ENTRIES))
    assert "#A83F2" in listed2.message
    count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert count == 1
    revs = (await session.execute(select(LedgerEntryRevision))).scalars().all()
    assert {r.change_type for r in revs} == {
        RevisionChangeType.DELETE.value,
        RevisionChangeType.RESTORE.value,
    }

    again_restore = await service.execute(
        "ou_user", ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref="A83F2")
    )
    assert "未被删除" in again_restore.message
    assert len((await session.execute(select(LedgerEntryRevision))).scalars().all()) == 2


async def test_user_isolation_same_short_id(session: AsyncSession) -> None:
    await _seed(session, user="ou_a", short_id="A83F2", amount="10")
    await _seed(session, user="ou_b", short_id="A83F2", amount="99", category="购物")
    service = LedgerService(session)
    await service.execute(
        "ou_a",
        ParsedCommand(action=Action.UPDATE_ENTRY, entry_ref="A83F2", amount=Decimal("11")),
    )
    a = (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.user_open_id == "ou_a", LedgerEntry.short_id == "A83F2"
            )
        )
    ).scalar_one()
    b = (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.user_open_id == "ou_b", LedgerEntry.short_id == "A83F2"
            )
        )
    ).scalar_one()
    assert a.amount == Decimal("11.00")
    assert b.amount == Decimal("99.00")
    denied = await service.execute(
        "ou_a", ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="ZZZZZ")
    )
    assert denied.message == "未找到该账目，或该账目不属于当前用户。"


async def test_update_last_and_undo_last_write_revisions(session: AsyncSession) -> None:
    service = LedgerService(session)
    await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("20"),
            direction=Direction.EXPENSE,
            category="餐饮",
            note="a",
            occurred_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
        ),
        source_message_id="om_1",
    )
    updated = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.UPDATE_LAST, amount=Decimal("21")),
    )
    assert "已修改 #" in updated.message
    undone = await service.execute("ou_user", ParsedCommand(action=Action.UNDO_LAST))
    assert "已撤销 #" in undone.message
    types = {
        r.change_type
        for r in (await session.execute(select(LedgerEntryRevision))).scalars().all()
    }
    assert RevisionChangeType.UPDATE.value in types
    assert RevisionChangeType.DELETE.value in types


def test_mutation_schema_and_deterministic_commands() -> None:
    assert ParsedCommand(
        action=Action.UPDATE_ENTRY, entry_ref="A83F2", amount=Decimal("1")
    ).action is Action.UPDATE_ENTRY
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.UPDATE_ENTRY, entry_ref="A83F2")
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.DELETE_ENTRY)
    delete = try_parse_deterministic_entry_command("删除 #a83f2")
    assert isinstance(delete, ParsedCommand)
    assert delete.action is Action.DELETE_ENTRY
    restore = try_parse_deterministic_entry_command("恢复 #A83F2")
    assert isinstance(restore, ParsedCommand)
    assert restore.action is Action.RESTORE_ENTRY
    assert try_parse_deterministic_entry_command("撤销刚才那笔") is None
    bound = bind_entry_refs_from_message(
        ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="ZZZZZ"),
        "删除 #A83F2",
    )
    assert isinstance(bound, ParsedCommand)
    assert bound.entry_ref == "A83F2"
