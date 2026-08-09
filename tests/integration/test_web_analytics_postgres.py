from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.models import Account, CategoryBudget, Direction, LedgerEntry
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.web_analytics import WebAnalyticsQueryService

pytestmark = pytest.mark.postgres


async def test_analytics_budget_and_export_isolation_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    occurred = datetime(2026, 8, 8, 4, tzinfo=UTC)
    async with postgres_session_factory() as session:
        contexts = {
            subject: await IdentityService(
                session, currency="CNY", timezone="Asia/Shanghai"
            ).resolve_or_bootstrap(channel="feishu", external_subject_id=subject)
            for subject in ("ou_a", "ou_b")
        }
        accounts = {
            subject: await session.scalar(
                select(Account.id).where(Account.ledger_id == context.ledger_id)
            )
            for subject, context in contexts.items()
        }
        assert all(accounts.values())
        session.add_all(
            [
                LedgerEntry(
                    user_open_id="ou_a",
                    ledger_id=contexts["ou_a"].ledger_id,
                    account_id=accounts["ou_a"],
                    short_id="PGA01",
                    amount=Decimal("48"),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="餐饮",
                    note="=formula",
                    occurred_at=occurred,
                    source_type="text",
                ),
                LedgerEntry(
                    user_open_id="ou_b",
                    ledger_id=contexts["ou_b"].ledger_id,
                    account_id=accounts["ou_b"],
                    short_id="PGA01",
                    amount=Decimal("999"),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="隐私",
                    note="private",
                    occurred_at=occurred,
                    source_type="text",
                ),
                CategoryBudget(
                    user_open_id="ou_a",
                    ledger_id=contexts["ou_a"].ledger_id,
                    category="餐饮",
                    amount=Decimal("200"),
                ),
            ]
        )
        await session.commit()
        query = WebAnalyticsQueryService(session, timezone="Asia/Shanghai", currency="CNY")
        summary, _, categories, _ = await query.analytics(
            contexts["ou_a"], start_date=date(2026, 8, 1), end_date=date(2026, 8, 8)
        )
        budgets = await query.budgets(contexts["ou_a"], now=datetime(2026, 8, 8, 8, tzinfo=UTC))
        exported = await LedgerService(session).execute(
            contexts["ou_a"],
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=datetime(2026, 8, 1, tzinfo=UTC),
                range_end=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        )
    assert summary.expense == Decimal("48")
    assert categories[0].category == "餐饮"
    assert budgets.total_spent == Decimal("48")
    assert exported.export is not None
    assert exported.export.row_count == 1
    assert b"'=formula" in exported.export.content
    assert b"999.00" not in exported.export.content
