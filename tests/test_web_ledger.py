from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import CategoryBudget, Direction, LedgerEntry
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.web_ledger import WebLedgerQueryService


async def _entry(
    session: AsyncSession,
    short_id: str,
    *,
    user: str = "ou_a",
    amount: str = "10",
    direction: Direction = Direction.EXPENSE,
    category: str = "餐饮",
    note: str = "午饭",
    days_ago: int = 0,
    deleted: bool = False,
) -> LedgerEntry:
    occurred_at = datetime(2026, 8, 8, 4, tzinfo=UTC) - timedelta(days=days_ago)
    row = LedgerEntry(
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
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_web_list_is_scoped_filtered_and_paginated(session: AsyncSession) -> None:
    await _entry(session, "AAAA1", amount="12", note="工作日午饭")
    await _entry(session, "AAAA2", amount="30", category="交通", days_ago=1)
    await _entry(session, "AAAA3", amount="99", deleted=True)
    await _entry(session, "AAAA1", user="ou_b", amount="888")
    service = WebLedgerQueryService(session)

    page = await service.list_entries("ou_a", page=1, page_size=1)
    assert page.total == 2
    assert page.pages == 2
    assert len(page.items) == 1
    searched = await service.list_entries(
        "ou_a", page=1, page_size=25, search="工作日", amount_max=Decimal("20")
    )
    assert [item.short_id for item in searched.items] == ["AAAA1"]
    deleted = await service.list_entries(
        "ou_a", page=1, page_size=25, deleted="deleted"
    )
    assert [item.short_id for item in deleted.items] == ["AAAA3"]


async def test_web_detail_revision_and_dashboard(session: AsyncSession) -> None:
    entry = await _entry(session, "BBBB1", amount="35")
    await _entry(
        session,
        "BBBB2",
        amount="100",
        direction=Direction.INCOME,
        category="工资",
    )
    session.add(CategoryBudget(user_open_id="ou_a", category="餐饮", amount=Decimal("200")))
    await session.commit()
    service = LedgerService(session)
    await service.execute(
        "ou_a",
        ParsedCommand(
            action=Action.UPDATE_ENTRY,
            entry_ref="BBBB1",
            amount=Decimal("40"),
        ),
        expected_updated_at=entry.updated_at,
    )

    query = WebLedgerQueryService(session)
    detail = await query.entry_detail("ou_a", "#bbbb1")
    assert detail is not None
    assert detail.entry.amount == Decimal("40")
    assert detail.revisions[0].before["amount"] == "35.00"
    assert await query.entry_detail("ou_b", "BBBB1") is None

    dashboard = await query.dashboard("ou_a", now=datetime(2026, 8, 8, 8, tzinfo=UTC))
    assert dashboard.month_income == Decimal("100")
    assert dashboard.month_expense == Decimal("40")
    assert dashboard.month_balance == Decimal("60")
    assert dashboard.budget_usage_rate == Decimal("20")
    assert dashboard.categories[0].category == "餐饮"
    assert len(dashboard.trend) == 30


async def test_web_entry_includes_ledger_scoped_account(session: AsyncSession) -> None:
    from lark_ledger.models import AccountType
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.identity import IdentityService

    context = await IdentityService(
        session, currency="CNY", timezone="Asia/Shanghai"
    ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_web_account")
    accounts = AccountService(session)
    wallet = await accounts.create(
        context, name="支付宝", account_type=AccountType.ASSET
    )
    created = await LedgerService(session, account_id=wallet.id).execute(
        context,
        ParsedCommand(
            action=Action.CREATE,
            amount=Decimal("32"),
            direction="expense",
            category="餐饮",
            occurred_at="2026-08-09T12:00:00+08:00",
        ),
    )
    short_id = created.message.split("#")[1][:5]
    query = WebLedgerQueryService(session)
    page = await query.list_entries(context, page=1, page_size=25)
    assert page.total == 1
    item = page.items[0]
    assert item.account_id == str(wallet.id)
    assert item.account_name == "支付宝"

    detail = await query.entry_detail(context, short_id)
    assert detail is not None
    assert detail.entry.account_name == "支付宝"

    dashboard = await query.dashboard(context, now=datetime(2026, 8, 9, 8, tzinfo=UTC))
    assert dashboard.recent_entries[0].account_id == str(wallet.id)


async def test_web_entry_unbound_account_stays_empty_string(session: AsyncSession) -> None:
    await _entry(session, "UNBD1", amount="9")
    query = WebLedgerQueryService(session)
    page = await query.list_entries("ou_a", page=1, page_size=25)
    assert page.total == 1
    assert page.items[0].account_id == ""
    assert page.items[0].account_name is None
