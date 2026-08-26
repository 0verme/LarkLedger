"""P46 — LedgerQueryService integration tests on PostgreSQL.

Proves the deterministic query contract against a real relational database:
ledger isolation, household access, private-account isolation (no leakage via
totals / counts / groups / Top N / pagination total), aggregation after privacy
filtering, pagination determinism across sessions, ``[start, end)`` boundary
semantics and transfer exclusion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountType,
    AccountVisibility,
    Direction,
    LedgerEntry,
)
from lark_ledger.query_schemas import (
    QueryGrouping,
    QueryIntent,
    QueryMode,
    QueryOrder,
    QuerySort,
    QueryStatus,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger_query import LedgerQueryService
from lark_ledger.services.query_planner import LedgerQueryAccessDeniedError
from lark_ledger.services.transfers import TransferService

pytestmark = pytest.mark.postgres

TZ = "Asia/Shanghai"


async def _identity(
    session: AsyncSession, open_id: str
) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone=TZ
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=open_id)


async def _entry(
    session: AsyncSession,
    context: RequestContext,
    *,
    short_id: str,
    amount: str,
    direction: Direction = Direction.EXPENSE,
    category: str = "餐饮",
    note: str = "",
    occurred_at: datetime | None = None,
    account_id: object | None = None,
) -> None:
    session.add(
        LedgerEntry(
            user_open_id=context.external_subject_id or "ou",
            created_by_user_id=context.actor_user_id,
            paid_by_user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            account_id=account_id,
            short_id=short_id,
            amount=Decimal(amount),
            currency="CNY",
            direction=direction,
            category=category,
            note=note,
            occurred_at=occurred_at or datetime(2026, 8, 8, 4, tzinfo=UTC),
            source_type="text",
        )
    )


async def test_ledger_isolation_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        ctx_a = await _identity(session, "ou_a")
        ctx_b = await _identity(session, "ou_b")
        accounts = {
            "a": await session.scalar(
                select(Account.id).where(Account.ledger_id == ctx_a.ledger_id)
            ),
            "b": await session.scalar(
                select(Account.id).where(Account.ledger_id == ctx_b.ledger_id)
            ),
        }
        assert all(accounts.values())
        await _entry(
            session, ctx_a, short_id="PG01", amount="100.00", account_id=accounts["a"]
        )
        await _entry(
            session, ctx_b, short_id="PG01", amount="999.00", account_id=accounts["b"]
        )
        await session.commit()

    async with postgres_session_factory() as session:
        svc = LedgerQueryService(session, timezone=TZ)
        # A sees only A's rows; totals are ledger-scoped.
        listing = await svc.query(ctx_a, QueryIntent(page_size=10))
        assert listing.status is QueryStatus.OK
        assert listing.total_count == 1
        assert listing.items[0].amount == Decimal("100.00")
        span = dict(
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 9, 1, tzinfo=UTC),
        )
        agg = await svc.query(ctx_a, QueryIntent(**span, mode=QueryMode.AGGREGATE))
        assert agg.aggregates is not None
        assert agg.aggregates.expense == Decimal("100.00")
        assert agg.aggregates.count == 1
        # A's actor on B's ledger must be denied at authorization time.
        foreign = RequestContext(
            actor_user_id=ctx_a.actor_user_id,
            ledger_id=ctx_b.ledger_id,
            source_channel="feishu",
            external_subject_id="ou_a",
        )
        with pytest.raises(LedgerQueryAccessDeniedError):
            await svc.query(foreign, QueryIntent(page_size=10))


async def test_household_access_and_private_isolation_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        owner = await _identity(session, "ou_owner")
        member = await _identity(session, "ou_member")
        manager = HouseholdManagementService(session, currency="CNY", timezone=TZ)
        home = await manager.create(owner.actor_user_id, "测试家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_owner",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_member",
        )
        shared = await session.scalar(
            select(Account).where(Account.ledger_id == owner_ctx.ledger_id)
        )
        assert shared is not None
        private = await AccountService(session).create(
            owner_ctx,
            name="私房钱",
            account_type=AccountType.CASH,
            currency="CNY",
            visibility=AccountVisibility.PRIVATE,
        )
        await _entry(
            session, owner_ctx, short_id="PH01", amount="100.00", account_id=shared.id
        )
        await _entry(
            session, owner_ctx, short_id="PH02", amount="999.00", account_id=private.id
        )
        await session.commit()

    async with postgres_session_factory() as session:
        svc = LedgerQueryService(session, timezone=TZ)
        span = dict(
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 9, 1, tzinfo=UTC),
        )
        # Household member: shared rows visible, private rows invisible in every
        # channel (list total, aggregate totals/count, group totals, top N).
        listing = await svc.query(member_ctx, QueryIntent(**span, page_size=10))
        assert listing.status is QueryStatus.OK
        assert listing.total_count == 1
        assert listing.items[0].short_id == "PH01"

        agg = await svc.query(member_ctx, QueryIntent(**span, mode=QueryMode.AGGREGATE))
        assert agg.aggregates is not None
        assert agg.aggregates.expense == Decimal("100.00")  # not 1099.00
        assert agg.aggregates.count == 1

        groups = await svc.query(
            member_ctx,
            QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.CATEGORY),
        )
        assert [(g.key, g.amount) for g in groups.groups or []] == [
            ("餐饮", Decimal("100.00"))
        ]
        assert groups.total_count == 1

        top = await svc.query(
            member_ctx,
            QueryIntent(
                **span,
                mode=QueryMode.GROUP,
                grouping=QueryGrouping.CATEGORY,
                top_n=1,
            ),
        )
        assert [g.key for g in top.groups or []] == ["餐饮"]

        # Owner (private-account owner) sees everything.
        owner_agg = await svc.query(owner_ctx, QueryIntent(**span, mode=QueryMode.AGGREGATE))
        assert owner_agg.aggregates is not None
        assert owner_agg.aggregates.expense == Decimal("1099.00")
        assert owner_agg.aggregates.count == 2


async def test_pagination_determinism_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        context = await _identity(session, "ou_page")
        account_id = await session.scalar(
            select(Account.id).where(Account.ledger_id == context.ledger_id)
        )
        base = datetime(2026, 8, 10, 4, tzinfo=UTC)
        for index in range(12):
            await _entry(
                session,
                context,
                short_id=f"PGP{index:02d}",
                amount=str(100 + index),
                account_id=account_id,
                occurred_at=base - timedelta(minutes=index),
            )
        await session.commit()

    async with postgres_session_factory() as first:
        svc = LedgerQueryService(first, timezone=TZ)
        intent = QueryIntent(
            page=1,
            page_size=5,
            sort=QuerySort.AMOUNT,
            order=QueryOrder.DESC,
        )
        page1 = await svc.query(context, intent)
        page2 = await svc.query(
            context,
            QueryIntent(page=2, page_size=5, sort=QuerySort.AMOUNT, order=QueryOrder.DESC),
        )
        assert page1.pagination is not None and page1.pagination.pages == 3
        expected_1 = [e.short_id for e in page1.items]
        expected_2 = [e.short_id for e in page2.items]
        assert page1.pagination.total == 12
        assert page1.items[0].short_id == "PGP11"  # highest amount first

    # A brand-new session (fresh connection) must reproduce the exact same
    # ordering — no natural DB order, no cross-page drift.
    async with postgres_session_factory() as second:
        svc2 = LedgerQueryService(second, timezone=TZ)
        again_1 = await svc2.query(
            context,
            QueryIntent(page=1, page_size=5, sort=QuerySort.AMOUNT, order=QueryOrder.DESC),
        )
        again_2 = await svc2.query(
            context,
            QueryIntent(page=2, page_size=5, sort=QuerySort.AMOUNT, order=QueryOrder.DESC),
        )
        assert [e.short_id for e in again_1.items] == expected_1
        assert [e.short_id for e in again_2.items] == expected_2
        assert not (set(expected_1) & set(expected_2))  # no overlap between pages


async def test_time_range_boundary_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        context = await _identity(session, "ou_range")
        account_id = await session.scalar(
            select(Account.id).where(Account.ledger_id == context.ledger_id)
        )
        start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        await _entry(
            session, context, short_id="PGB01", amount="1.00", account_id=account_id,
            occurred_at=start,  # exactly at start → included
        )
        await _entry(
            session, context, short_id="PGB02", amount="2.00", account_id=account_id,
            occurred_at=end - timedelta(seconds=1),  # inside → included
        )
        await _entry(
            session, context, short_id="PGB03", amount="3.00", account_id=account_id,
            occurred_at=end,  # exactly at end → excluded
        )
        await session.commit()

    async with postgres_session_factory() as session:
        svc = LedgerQueryService(session, timezone=TZ)
        result = await svc.query(
            context,
            QueryIntent(
                start=start, end=end, page_size=10, sort="amount", order="asc"
            ),
        )
        assert [e.short_id for e in result.items] == ["PGB01", "PGB02"]
        assert result.total_count == 2
        assert result.period is not None
        assert result.period.start == start
        assert result.period.end == end


async def test_transfers_excluded_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with postgres_session_factory() as session:
        context = await _identity(session, "ou_tf")
        default_id = await session.scalar(
            select(Account.id).where(Account.ledger_id == context.ledger_id)
        )
        second = await AccountService(session).create(
            context, name="储蓄卡", account_type=AccountType.CASH, currency="CNY"
        )
        await session.commit()
        await TransferService(session).create(
            context,
            from_account_id=default_id,
            to_account_id=second.id,
            amount=Decimal("300.00"),
            occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
            source_type="client",
        )
        await session.commit()

    async with postgres_session_factory() as session:
        svc = LedgerQueryService(session, timezone=TZ)
        span = dict(
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 9, 1, tzinfo=UTC),
        )
        # A transfer is not an expense/income entry: it never appears in list,
        # aggregate or group results.
        listing = await svc.query(context, QueryIntent(**span, page_size=10))
        assert listing.status is QueryStatus.EMPTY
        agg = await svc.query(context, QueryIntent(**span, mode=QueryMode.AGGREGATE))
        assert agg.status is QueryStatus.EMPTY
        groups = await svc.query(
            context,
            QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.CATEGORY),
        )
        assert groups.status is QueryStatus.EMPTY