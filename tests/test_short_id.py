from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.models import Base, Direction, LedgerEntry
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger_management import LedgerManagementService
from lark_ledger.short_id import (
    CROCKFORD_ALPHABET,
    SHORT_ID_LENGTH,
    ShortIdError,
    format_entry_ref,
    generate_short_id,
    is_valid_short_id,
    normalize_entry_ref,
)


def test_generate_short_id_length_and_charset() -> None:
    for _ in range(50):
        value = generate_short_id()
        assert len(value) == SHORT_ID_LENGTH
        assert all(ch in CROCKFORD_ALPHABET for ch in value)
        assert not any(ch in "ILOU" for ch in value)


def test_format_and_normalize_entry_ref() -> None:
    assert format_entry_ref("A83F2") == "#A83F2"
    assert normalize_entry_ref("#a83f2") == "A83F2"
    assert normalize_entry_ref("a83f2") == "A83F2"
    assert normalize_entry_ref(" A83F2 ") == "A83F2"
    assert is_valid_short_id("#7k2mw")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "A83F",
        "A83F21",
        "#A83F",
        "A83FI",
        "A83FO",
        "A83FL",
        "A83FU",
        "##A83F2",
        "####",
    ],
)
def test_normalize_rejects_invalid_refs(value: str) -> None:
    with pytest.raises(ShortIdError):
        normalize_entry_ref(value)


def _entry(
    user: str, short_id: str, amount: str, category: str, *, ledger_id: object = None
) -> LedgerEntry:
    return LedgerEntry(
        user_open_id=user,
        ledger_id=ledger_id,  # type: ignore[arg-type]
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=Direction.EXPENSE,
        category=category,
        note="",
        occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        source_type="text",
    )


async def test_ledger_scoped_unique_and_cross_ledger_reuse(session: AsyncSession) -> None:
    identities = IdentityService(session, currency="CNY", timezone="Asia/Shanghai")
    context = await identities.resolve_or_bootstrap(
        channel="feishu", external_subject_id="ou_a"
    )
    second = await LedgerManagementService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).create(context.actor_user_id, "旅行")
    second_id = second.id
    session.add(_entry("ou_a", "AAAAA", "1.00", "餐饮", ledger_id=context.ledger_id))
    await session.commit()

    session.add(_entry("ou_a", "AAAAA", "2.00", "交通", ledger_id=context.ledger_id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    first = (await session.execute(select(LedgerEntry))).scalar_one()
    first.deleted_at = datetime(2026, 8, 5, 13, tzinfo=UTC)
    await session.commit()

    session.add(_entry("ou_a", "AAAAA", "3.00", "其他", ledger_id=context.ledger_id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    session.add(_entry("ou_a", "AAAAA", "4.00", "工资", ledger_id=second_id))
    await session.commit()
    count = len((await session.execute(select(LedgerEntry))).scalars().all())
    assert count == 2


async def test_short_id_required_not_null() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            LedgerEntry(
                user_open_id="ou_null",
                short_id=None,  # type: ignore[arg-type]
                amount=Decimal("1.00"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="餐饮",
                note="",
                occurred_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                source_type="text",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    await engine.dispose()
