from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.models import Account, Direction, LedgerEntry, LedgerEntryRevision
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import EntryConflictError, LedgerService
from lark_ledger.services.web_ledger import WebLedgerQueryService

pytestmark = pytest.mark.postgres


async def test_web_ledger_postgres_scope_filters_revision_and_version_lock(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
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
                    short_id="A83F2",
                    amount=Decimal("32"),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="餐饮",
                    note="午饭",
                    occurred_at=now,
                    source_type="text",
                ),
                LedgerEntry(
                    user_open_id="ou_a",
                    ledger_id=contexts["ou_a"].ledger_id,
                    account_id=accounts["ou_a"],
                    short_id="B83F2",
                    amount=Decimal("80"),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="交通",
                    note="打车",
                    occurred_at=now - timedelta(days=1),
                    source_type="image",
                ),
                LedgerEntry(
                    user_open_id="ou_b",
                    ledger_id=contexts["ou_b"].ledger_id,
                    account_id=accounts["ou_b"],
                    short_id="A83F2",
                    amount=Decimal("999"),
                    currency="CNY",
                    direction=Direction.INCOME,
                    category="工资",
                    note="不可见",
                    occurred_at=now,
                    source_type="text",
                ),
            ]
        )
        await session.commit()
        page = await WebLedgerQueryService(session).list_entries(
            contexts["ou_a"],
            page=1,
            page_size=1,
            category="餐饮",
            amount_min=Decimal("30"),
        )
        assert page.total == 1
        assert [entry.short_id for entry in page.items] == ["A83F2"]
        detail = await WebLedgerQueryService(session).entry_detail(contexts["ou_b"], "B83F2")
        assert detail is None
        row = await session.scalar(
            select(LedgerEntry).where(
                LedgerEntry.user_open_id == "ou_a",
                LedgerEntry.short_id == "A83F2",
            )
        )
        assert row is not None
        version = row.updated_at

    async with postgres_session_factory() as first:
        await LedgerService(first).execute(
            contexts["ou_a"],
            ParsedCommand(
                action=Action.UPDATE_ENTRY,
                entry_ref="A83F2",
                amount=Decimal("35"),
            ),
            expected_updated_at=version,
        )
    async with postgres_session_factory() as stale:
        with pytest.raises(EntryConflictError):
            await LedgerService(stale).execute(
                contexts["ou_a"],
                ParsedCommand(
                    action=Action.UPDATE_ENTRY,
                    entry_ref="A83F2",
                    amount=Decimal("36"),
                ),
                expected_updated_at=version,
            )
        await stale.rollback()
    async with postgres_session_factory() as verify:
        entry = await WebLedgerQueryService(verify).entry_detail(contexts["ou_a"], "A83F2")
        assert entry is not None
        assert entry.entry.amount == Decimal("35")
        revisions = (await verify.scalars(select(LedgerEntryRevision))).all()
        assert len(revisions) == 1
