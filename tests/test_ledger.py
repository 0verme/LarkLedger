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
