from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.entry_commands import (
    bind_entry_refs_from_message,
    try_parse_deterministic_entry_command,
)
from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ledger import LedgerService
from lark_ledger.short_id import extract_entry_refs, format_entry_ref


async def _create(
    session: AsyncSession,
    *,
    user: str,
    short_id: str,
    amount: str,
    category: str,
    occurred_at: datetime,
    direction: Direction = Direction.EXPENSE,
    note: str = "",
    deleted: bool = False,
    created_at: datetime | None = None,
) -> LedgerEntry:
    entry = LedgerEntry(
        user_open_id=user,
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=direction,
        category=category,
        note=note,
        occurred_at=occurred_at,
        source_type="text",
        deleted_at=occurred_at if deleted else None,
    )
    session.add(entry)
    await session.flush()
    if created_at is not None:
        entry.created_at = created_at
        entry.updated_at = created_at
    await session.commit()
    return entry


async def test_list_entries_default_excludes_deleted_and_is_user_scoped(
    session: AsyncSession,
) -> None:
    base = datetime(2026, 8, 5, 12, tzinfo=UTC)
    await _create(
        session, user="ou_a", short_id="AAAAA", amount="10", category="餐饮", occurred_at=base
    )
    await _create(
        session,
        user="ou_a",
        short_id="AAAAB",
        amount="20",
        category="交通",
        occurred_at=base + timedelta(hours=1),
        deleted=True,
    )
    await _create(
        session,
        user="ou_b",
        short_id="AAAAA",
        amount="99",
        category="购物",
        occurred_at=base + timedelta(hours=2),
    )

    result = await LedgerService(session).execute(
        "ou_a", ParsedCommand(action=Action.LIST_ENTRIES)
    )
    assert "最近 1 笔账目" in result.message
    assert "#AAAAA" in result.message
    assert "餐饮" in result.message
    assert "#AAAAB" not in result.message
    assert "购物" not in result.message
    assert "99" not in result.message


async def test_list_entries_limit_cap_category_and_keyset_pagination(
    session: AsyncSession,
) -> None:
    base = datetime(2026, 8, 1, 10, tzinfo=UTC)
    for index, code in enumerate(["AAAA1", "AAAA2", "AAAA3", "AAAA4", "AAAA5"]):
        await _create(
            session,
            user="ou_user",
            short_id=code,
            amount=str(index + 1),
            category="餐饮" if index % 2 == 0 else "交通",
            occurred_at=base + timedelta(hours=index),
            note="这是一条超过二十个字符的备注用来验证列表截断效果",
            created_at=base + timedelta(minutes=index),
        )

    service = LedgerService(session)
    first_page = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.LIST_ENTRIES, limit=2, category="餐饮"),
    )
    assert "最近 2 笔账目" in first_page.message
    assert "1. #AAAA5" in first_page.message
    assert "2. #AAAA3" in first_page.message
    assert "交通" not in first_page.message
    assert "…" in first_page.message
    assert "查看 #AAAA3 之前的2笔" in first_page.message

    second_page = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.LIST_ENTRIES,
            limit=2,
            category="餐饮",
            before_entry_ref="#AAAA3",
        ),
    )
    assert "最近 1 笔账目" in second_page.message
    assert "#AAAA1" in second_page.message
    assert "#AAAA5" not in second_page.message
    assert "查看 #AAAA3 之前" not in second_page.message

    capped = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.LIST_ENTRIES, limit=50),
    )
    assert "单次最多显示 20 笔" in capped.message


async def test_keyset_stable_with_identical_occurred_at(session: AsyncSession) -> None:
    when = datetime(2026, 8, 5, 12, tzinfo=UTC)
    # Same occurred_at; created_at / id break ties.
    for index, code in enumerate(["SAME1", "SAME2", "SAME3"]):
        await _create(
            session,
            user="ou_user",
            short_id=code,
            amount=str(index + 1),
            category="餐饮",
            occurred_at=when,
            created_at=when + timedelta(seconds=index),
        )

    service = LedgerService(session)
    page1 = await service.execute(
        "ou_user", ParsedCommand(action=Action.LIST_ENTRIES, limit=2)
    )
    assert "1. #SAME3" in page1.message
    assert "2. #SAME2" in page1.message
    assert "查看 #SAME2 之前的2笔" in page1.message

    page2 = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.LIST_ENTRIES, limit=2, before_entry_ref="SAME2"),
    )
    assert "1. #SAME1" in page2.message
    assert "#SAME2" not in page2.message.split("继续查看", maxsplit=1)[0]
    assert "#SAME3" not in page2.message.split("继续查看", maxsplit=1)[0]

    # Insert a newer row; old cursor still returns older data only.
    await _create(
        session,
        user="ou_user",
        short_id="SAME4",
        amount="9",
        category="餐饮",
        occurred_at=when + timedelta(hours=1),
        created_at=when + timedelta(hours=1),
    )
    page2_again = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.LIST_ENTRIES, limit=2, before_entry_ref="SAME2"),
    )
    assert "#SAME1" in page2_again.message
    assert "#SAME4" not in page2_again.message


async def test_list_entries_empty_and_invalid_cursor(session: AsyncSession) -> None:
    service = LedgerService(session)
    empty = await service.execute("ou_user", ParsedCommand(action=Action.LIST_ENTRIES))
    assert empty.message == "没有符合条件的账目。"

    bad = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.LIST_ENTRIES, before_entry_ref="BAD"),
    )
    assert "分页短 ID 格式无效" in bad.message

    missing = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.LIST_ENTRIES, before_entry_ref="#AAAAA"),
    )
    assert "未找到该账目，或该账目不属于当前用户" in missing.message


async def test_get_entry_shows_detail_including_deleted(session: AsyncSession) -> None:
    when = datetime(2026, 8, 5, 4, 30, tzinfo=UTC)
    entry = await _create(
        session,
        user="ou_user",
        short_id="A83F2",
        amount="32.5",
        category="餐饮",
        occurred_at=when,
        note="午饭",
        deleted=True,
    )
    service = LedgerService(session, timezone="Asia/Shanghai")
    result = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="#a83f2"),
    )
    assert format_entry_ref("A83F2") in result.message
    assert "状态：已删除" in result.message
    assert "删除时间：" in result.message
    assert "支出" in result.message
    assert "¥32.50" in result.message
    assert "午饭" in result.message
    assert "来源类型：text" in result.message
    assert str(entry.id) not in result.message
    assert "ou_user" not in result.message

    missing = await service.execute(
        "ou_other",
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="A83F2"),
    )
    assert missing.message == "未找到该账目，或该账目不属于当前用户。"

    invalid = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="#OIIII"),
    )
    assert "短 ID 格式无效" in invalid.message


def test_deterministic_entry_commands() -> None:
    get_cmd = try_parse_deterministic_entry_command("查看 #a83f2")
    assert isinstance(get_cmd, ParsedCommand)
    assert get_cmd.action is Action.GET_ENTRY
    assert get_cmd.entry_ref == "A83F2"

    page = try_parse_deterministic_entry_command("查看 #7K2MW 之前的10笔")
    assert isinstance(page, ParsedCommand)
    assert page.action is Action.LIST_ENTRIES
    assert page.before_entry_ref == "7K2MW"
    assert page.limit == 10

    multi = try_parse_deterministic_entry_command("查看 #AAAAA 和 #BBBBB")
    assert isinstance(multi, str)
    assert "一次只能查看一个" in multi

    assert try_parse_deterministic_entry_command("最近10笔") is None


def test_bind_entry_refs_rejects_invented_ids() -> None:
    bound = bind_entry_refs_from_message(
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="ZZZZZ"),
        "查看一下账本",
    )
    assert isinstance(bound, str)

    fixed = bind_entry_refs_from_message(
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="ZZZZZ"),
        "查看 #A83F2",
    )
    assert isinstance(fixed, ParsedCommand)
    assert fixed.entry_ref == "A83F2"
    assert extract_entry_refs("查看 #a83f2 和 x") == ["A83F2"]

    multi = bind_entry_refs_from_message(
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="AAAAA"),
        "查看 #AAAAA 和 #BBBBB",
    )
    assert isinstance(multi, str)

    page = bind_entry_refs_from_message(
        ParsedCommand(
            action=Action.LIST_ENTRIES,
            before_entry_ref="ZZZZZ",
            limit=10,
        ),
        "查看 #A83F2 之前的10笔",
    )
    assert isinstance(page, ParsedCommand)
    assert page.before_entry_ref == "A83F2"

    page_bad = bind_entry_refs_from_message(
        ParsedCommand(action=Action.LIST_ENTRIES, before_entry_ref="ZZZZZ", limit=10),
        "查看最近账单",
    )
    assert isinstance(page_bad, str)

    create_cmd = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("1"),
        direction=Direction.EXPENSE,
        category="餐饮",
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert bind_entry_refs_from_message(create_cmd, "午饭1") is create_cmd


def test_list_and_get_schema_validation() -> None:
    assert ParsedCommand(action=Action.LIST_ENTRIES, limit=50).limit == 50
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.GET_ENTRY)
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.GET_ENTRY, entry_ref="A83F2", category="餐饮")
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.SUMMARY,
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
            range_end=datetime(2026, 9, 1, tzinfo=UTC),
            limit=10,
        )
