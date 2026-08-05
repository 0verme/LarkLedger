from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.config import Settings
from lark_ledger.models import CategoryBudget, Direction, LedgerEntry
from lark_ledger.schemas import Action, EntryCandidate, ParsedCommand
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError
from lark_ledger.services.ledger import LedgerService


async def test_short_id_conflict_retries_then_succeeds(session: AsyncSession) -> None:
    session.add(
        LedgerEntry(
            user_open_id="ou_user",
            short_id="AAAAA",
            amount=Decimal("1.00"),
            currency="CNY",
            direction=Direction.EXPENSE,
            category="占位",
            note="",
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            source_type="text",
        )
    )
    await session.commit()

    codes = iter(["AAAAA", "BBBBB"])
    service = LedgerService(session, short_id_factory=lambda: next(codes))
    result = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("32"),
            direction=Direction.EXPENSE,
            category="餐饮",
            note="午饭",
            occurred_at=datetime(2026, 8, 2, 4, tzinfo=UTC),
        ),
        source_message_id="om_retry",
    )
    assert "#BBBBB" in result.message
    rows = (
        await session.execute(select(LedgerEntry).order_by(LedgerEntry.amount))
    ).scalars().all()
    assert [row.short_id for row in rows] == ["AAAAA", "BBBBB"]


async def test_short_id_conflict_exhaustion_fails_without_orphan_row(
    session: AsyncSession,
) -> None:
    session.add(
        LedgerEntry(
            user_open_id="ou_user",
            short_id="AAAAA",
            amount=Decimal("1.00"),
            currency="CNY",
            direction=Direction.EXPENSE,
            category="占位",
            note="",
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            source_type="text",
        )
    )
    await session.commit()

    service = LedgerService(session, short_id_factory=lambda: "AAAAA")
    with pytest.raises(RuntimeError, match="failed to allocate short_id"):
        await service.execute(
            "ou_user",
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("32"),
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=datetime(2026, 8, 2, 4, tzinfo=UTC),
            ),
            source_message_id="om_exhaust",
        )
    await session.rollback()
    count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert count == 1


async def test_batch_assigns_distinct_short_ids_and_isolates_conflict(
    session: AsyncSession,
) -> None:
    session.add(
        LedgerEntry(
            user_open_id="ou_user",
            short_id="AAAAA",
            amount=Decimal("1.00"),
            currency="CNY",
            direction=Direction.EXPENSE,
            category="占位",
            note="",
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            source_type="text",
        )
    )
    await session.commit()

    # First new entry collides once then succeeds; second entry gets a fresh code.
    codes = iter(["AAAAA", "BBBBB", "CCCCC"])
    service = LedgerService(session, short_id_factory=lambda: next(codes))
    result = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.CREATE_ENTRIES,
            entries=[
                EntryCandidate(
                    amount="10",
                    direction="expense",
                    category="餐饮",
                    occurred_at="2026-08-02T12:00:00+00:00",
                ),
                EntryCandidate(
                    amount="20",
                    direction="expense",
                    category="交通",
                    occurred_at="2026-08-02T13:00:00+00:00",
                ),
            ],
        ),
        source_type="image",
        source_message_id="om_batch_short",
    )
    assert "#BBBBB" in result.message
    assert "#CCCCC" in result.message
    assert "成功 2 笔" in result.message
    rows = (
        await session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.source_message_id == "om_batch_short")
            .order_by(LedgerEntry.source_item_index)
        )
    ).scalars().all()
    assert [row.short_id for row in rows] == ["BBBBB", "CCCCC"]


async def test_create_update_and_undo(session: AsyncSession) -> None:
    service = LedgerService(session)
    created = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("32"),
            direction=Direction.EXPENSE,
            category="餐饮",
            note="午饭",
            occurred_at=datetime(2026, 8, 2, 4, tzinfo=UTC),
        ),
        source_message_id="om_1",
    )
    assert "¥32.00" in created.message
    entry = (await session.execute(select(LedgerEntry))).scalar_one()
    assert entry.short_id is not None
    assert len(entry.short_id) == 5
    assert f"#{entry.short_id}" in created.message

    updated = await service.execute(
        "ou_user", ParsedCommand(action=Action.UPDATE_LAST, amount=Decimal("8"))
    )
    assert "¥8.00" in updated.message
    assert f"#{entry.short_id}" in updated.message

    undone = await service.execute("ou_user", ParsedCommand(action=Action.UNDO_LAST))
    assert "已撤销" in undone.message
    assert f"#{entry.short_id}" in undone.message
    entry = (await session.execute(select(LedgerEntry))).scalar_one()
    assert entry.deleted_at is not None


async def test_foreign_currency_create_update_and_budget_use_converted_amount(
    session: AsyncSession,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        rates = {"JPY": 0.04781, "USD": 7.2}
        source = request.url.path.split("/")[-2]
        return httpx.Response(
            200,
            json={
                "date": "2026-08-03",
                "base": source,
                "quote": "CNY",
                "rate": rates[source],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://rates.example"
    )
    exchange_rates = ExchangeRateService(Settings(_env_file=None), client)
    service = LedgerService(session, exchange_rates=exchange_rates)
    created = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("1300"),
            currency="JPY",
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime(2026, 8, 2, 4, tzinfo=UTC),
        ),
    )
    assert "¥62.15（由 1300.00 JPY 约算）" in created.message

    updated = await service.execute(
        "ou_user",
        ParsedCommand(action=Action.UPDATE_LAST, amount=Decimal("10"), currency="USD"),
    )
    assert "¥72.00（由 10.00 USD 约算）" in updated.message

    budget = await service.execute(
        "ou_user",
        ParsedCommand(
            action=Action.SET_BUDGET,
            amount=Decimal("20"),
            currency="USD",
            category="餐饮",
        ),
    )
    assert "¥144.00（由 20.00 USD 约算）" in budget.message
    entry = (await session.execute(select(LedgerEntry))).scalar_one()
    stored_budget = (await session.execute(select(CategoryBudget))).scalar_one()
    assert entry.amount == Decimal("72.00")
    assert entry.currency == "CNY"
    assert stored_budget.amount == Decimal("144.00")
    await client.aclose()


@pytest.mark.parametrize("rate", ["0.000001", "999999999999.99"])
async def test_invalid_converted_amount_does_not_write(
    session: AsyncSession, rate: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"date": "2026-08-03", "base": "JPY", "quote": "CNY", "rate": rate},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://rates.example"
    )
    service = LedgerService(
        session,
        exchange_rates=ExchangeRateService(Settings(_env_file=None), client),
    )
    with pytest.raises(ValueError):
        await service.execute(
            "ou_user",
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("100"),
                currency="JPY",
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=datetime(2026, 8, 2, 4, tzinfo=UTC),
            ),
        )
    count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert count == 0
    await client.aclose()


async def test_unavailable_rate_does_not_write(session: AsyncSession) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        base_url="https://rates.example",
    )
    service = LedgerService(
        session,
        exchange_rates=ExchangeRateService(Settings(_env_file=None), client),
    )
    with pytest.raises(ExchangeRateUnavailableError):
        await service.execute(
            "ou_user",
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal("1300"),
                currency="JPY",
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=datetime(2026, 8, 2, 4, tzinfo=UTC),
            ),
        )
    count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert count == 0
    await client.aclose()


async def test_summary_is_isolated_by_user(session: AsyncSession) -> None:
    service = LedgerService(session)
    occurred_at = datetime(2026, 8, 2, 4, tzinfo=UTC)
    for user, amount in (("ou_a", "10"), ("ou_a", "20"), ("ou_b", "999")):
        await service.execute(
            user,
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal(amount),
                direction=Direction.EXPENSE,
                category="餐饮",
                occurred_at=occurred_at,
            ),
        )
    summary = await service.execute(
        "ou_a",
        ParsedCommand(
            action=Action.SUMMARY,
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
            range_end=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )
    assert "¥30.00" in summary.message
    assert "999" not in summary.message


async def test_report_aggregates_expenses_income_and_local_days(session: AsyncSession) -> None:
    service = LedgerService(session, timezone="Asia/Shanghai")
    entries = (
        ("ou_a", "10.25", Direction.EXPENSE, "餐饮", datetime(2026, 8, 1, 16, tzinfo=UTC)),
        ("ou_a", "20.75", Direction.EXPENSE, "交通", datetime(2026, 8, 2, 4, tzinfo=UTC)),
        ("ou_a", "100", Direction.INCOME, "工资", datetime(2026, 8, 2, 5, tzinfo=UTC)),
        ("ou_b", "999", Direction.EXPENSE, "其他", datetime(2026, 8, 2, 5, tzinfo=UTC)),
    )
    for user, amount, direction, category, occurred_at in entries:
        await service.execute(
            user,
            ParsedCommand(
                action=Action.CREATE,
                amount=Decimal(amount),
                direction=direction,
                category=category,
                occurred_at=occurred_at,
            ),
        )

    result = await service.execute(
        "ou_a",
        ParsedCommand(
            action=Action.REPORT,
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
            range_end=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )

    assert result.report is not None
    assert result.report.expense_total == Decimal("31.00")
    assert result.report.income_total == Decimal("100.00")
    assert result.report.balance == Decimal("69.00")
    assert result.report.entry_count == 3
    assert [item.category for item in result.report.categories] == ["交通", "餐饮"]
    assert result.report.trend[0].period.isoformat() == "2026-08-01"
    assert result.report.trend[0].amount == Decimal("0")
    assert result.report.trend[1].amount == Decimal("31.00")


async def test_report_uses_monthly_trend_and_rejects_ranges_over_366_days(
    session: AsyncSession,
) -> None:
    service = LedgerService(session)
    await service.execute(
        "ou_a",
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("12"),
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
        ),
    )
    result = await service.execute(
        "ou_a",
        ParsedCommand(
            action=Action.REPORT,
            range_start=datetime(2026, 1, 1, tzinfo=UTC),
            range_end=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )
    assert result.report is not None
    assert result.report.trend_granularity == "month"

    too_long = await service.execute(
        "ou_a",
        ParsedCommand(
            action=Action.REPORT,
            range_start=datetime(2025, 1, 1, tzinfo=UTC),
            range_end=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )
    assert too_long.report is None
    assert "366" in too_long.message
