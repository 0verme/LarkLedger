from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ledger import LedgerService


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

    updated = await service.execute(
        "ou_user", ParsedCommand(action=Action.UPDATE_LAST, amount=Decimal("8"))
    )
    assert "¥8.00" in updated.message

    undone = await service.execute("ou_user", ParsedCommand(action=Action.UNDO_LAST))
    assert "已撤销" in undone.message
    entry = (await session.execute(select(LedgerEntry))).scalar_one()
    assert entry.deleted_at is not None


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
