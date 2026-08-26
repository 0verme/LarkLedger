"""P46 — LedgerQueryService unit tests (SQLite).

Covers the deterministic query semantics: stable list + pagination, exact
Decimal aggregation, grouping (category/account/day/week/month), Top N with
deterministic tie-breaks, ``[start, end)`` timezone boundaries, controlled
empty / invalid / needs_clarification / unsupported outcomes, ledger isolation,
privacy-before-aggregation and transfer exclusion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    QueryPlan,
    QueryResult,
    QuerySort,
    QueryStatus,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.household_management import HouseholdManagementService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger_query import LedgerQueryService
from lark_ledger.services.query_planner import (
    LedgerQueryAccessDeniedError,
    QueryPlanner,
)
from lark_ledger.services.transfers import TransferService

TZ = "Asia/Shanghai"
TIMEZONE = ZoneInfo(TZ)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


async def _identity(session: AsyncSession, open_id: str, name: str) -> RequestContext:
    return await IdentityService(
        session, currency="CNY", timezone=TZ
    ).resolve_or_bootstrap(channel="feishu", external_subject_id=open_id, display_name=name)


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
    account_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> LedgerEntry:
    row = LedgerEntry(
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
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.flush()
    return row


async def _default_account(
    session: AsyncSession, context: RequestContext
) -> Account:
    account = await session.scalar(
        select(Account).where(Account.ledger_id == context.ledger_id)
    )
    assert account is not None
    return account


async def _household(
    session: AsyncSession,
) -> tuple[RequestContext, RequestContext, dict[str, Account]]:
    """Household with one shared default account + an owner-only private account."""
    owner = await _identity(session, "ou_owner", "A")
    member = await _identity(session, "ou_member", "B")
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
    shared = await _default_account(session, owner_ctx)
    private = await AccountService(session).create(
        owner_ctx,
        name="私房钱",
        account_type=AccountType.CASH,
        currency="CNY",
        visibility=AccountVisibility.PRIVATE,
    )
    await session.commit()
    return owner_ctx, member_ctx, {"shared": shared, "private": private}


def _svc(session: AsyncSession) -> LedgerQueryService:
    return LedgerQueryService(session, timezone=TZ, currency="CNY")


# --------------------------------------------------------------------------- #
# Entry list: filters + stable pagination
# --------------------------------------------------------------------------- #


async def test_list_filters_and_stable_pagination(session: AsyncSession) -> None:
    context = await _identity(session, "ou_a", "A")
    account = await _default_account(session, context)
    now = datetime(2026, 8, 8, 4, tzinfo=UTC)
    for index, (amount, direction, category, note) in enumerate(
        [
            ("120.00", Direction.EXPENSE, "餐饮", "午饭"),
            ("80.00", Direction.EXPENSE, "交通", "打车"),
            ("10000.00", Direction.INCOME, "工资", "工资到账"),
            ("500.50", Direction.EXPENSE, "餐饮", "晚饭"),
        ]
    ):
        await _entry(
            session,
            context,
            short_id=f"E{index:04d}",
            amount=amount,
            direction=direction,
            category=category,
            note=note,
            occurred_at=now - timedelta(days=index),
            account_id=account.id,
        )
    await session.commit()

    svc = _svc(session)
    # expense only, min amount >= 100, keyword "饭" matches both 午饭/晚饭 notes
    result = await svc.query(
        context,
        QueryIntent(
            mode=QueryMode.LIST,
            direction=Direction.EXPENSE,
            min_amount=Decimal("100"),
            keyword="饭",
            page_size=2,
        ),
    )
    assert result.status is QueryStatus.OK
    assert result.total_count == 2
    assert result.pagination is not None
    assert result.pagination.pages == 1
    # occurred_at desc → newest (E0000) first
    assert [entry.short_id for entry in result.items] == ["E0000", "E0003"]
    assert result.items[0].amount == Decimal("120.00")

    # keyword matches category too
    result2 = await svc.query(
        context, QueryIntent(mode=QueryMode.LIST, keyword="交通", page_size=10)
    )
    assert result2.total_count == 1
    assert result2.items[0].category == "交通"

    # category + account filter by server-resolved name
    result3 = await svc.query(
        context,
        QueryIntent(
            mode=QueryMode.LIST, category="餐饮", account="默认账户", page_size=10
        ),
    )
    assert result3.total_count == 2

    # amount max bound
    result4 = await svc.query(
        context,
        QueryIntent(
            mode=QueryMode.LIST,
            direction=Direction.EXPENSE,
            max_amount=Decimal("100"),
            page_size=10,
        ),
    )
    assert result4.total_count == 1
    assert result4.items[0].short_id == "E0001"


async def test_list_tiebreak_determinism(session: AsyncSession) -> None:
    context = await _identity(session, "ou_tie", "A")
    account = await _default_account(session, context)
    same_time = datetime(2026, 8, 8, 4, tzinfo=UTC)
    await _entry(
        session,
        context,
        short_id="TAAAA",
        amount="1.00",
        account_id=account.id,
        occurred_at=same_time,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    await _entry(
        session,
        context,
        short_id="TBBBB",
        amount="1.00",
        account_id=account.id,
        occurred_at=same_time,
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    await session.commit()
    result = await _svc(session).query(context, QueryIntent(page_size=10))
    # Same occurred_at → created_at desc tie-break → TBBBB first.
    assert [entry.short_id for entry in result.items] == ["TBBBB", "TAAAA"]


async def test_list_pagination_pages(session: AsyncSession) -> None:
    context = await _identity(session, "ou_page", "A")
    account = await _default_account(session, context)
    for index in range(7):
        await _entry(
            session,
            context,
            short_id=f"P{index:04d}",
            amount=str(10 + index),
            account_id=account.id,
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC) - timedelta(days=index),
        )
    await session.commit()
    page1 = await _svc(session).query(context, QueryIntent(page=1, page_size=3))
    page2 = await _svc(session).query(context, QueryIntent(page=2, page_size=3))
    page3 = await _svc(session).query(context, QueryIntent(page=3, page_size=3))
    assert page1.pagination is not None and page1.pagination.pages == 3
    assert [e.short_id for e in page1.items] == ["P0000", "P0001", "P0002"]
    assert [e.short_id for e in page2.items] == ["P0003", "P0004", "P0005"]
    assert [e.short_id for e in page3.items] == ["P0006"]
    assert page1.pagination.total == 7


# --------------------------------------------------------------------------- #
# Aggregate total: Decimal exactness
# --------------------------------------------------------------------------- #


async def test_aggregate_exact_decimal_totals(session: AsyncSession) -> None:
    context = await _identity(session, "ou_agg", "A")
    account = await _default_account(session, context)
    await _entry(session, context, short_id="A0001", amount="0.10", account_id=account.id)
    await _entry(session, context, short_id="A0002", amount="0.20", account_id=account.id)
    await _entry(session, context, short_id="A0003", amount="0.70", account_id=account.id)
    await _entry(
        session,
        context,
        short_id="A0004",
        amount="1000.00",
        direction=Direction.INCOME,
        account_id=account.id,
    )
    await session.commit()
    span = {
        "start": datetime(2026, 8, 1, tzinfo=UTC),
        "end": datetime(2026, 9, 1, tzinfo=UTC),
    }
    only_expense = await _svc(session).query(
        context,
        QueryIntent(**span, mode=QueryMode.AGGREGATE, direction=Direction.EXPENSE),
    )
    assert only_expense.status is QueryStatus.OK
    assert only_expense.aggregates is not None
    assert only_expense.aggregates.expense == Decimal("1.00")  # 0.1 + 0.2 + 0.7 exactly
    assert only_expense.aggregates.income == Decimal("0")
    assert only_expense.aggregates.count == 3

    both = await _svc(session).query(
        context, QueryIntent(**span, mode=QueryMode.AGGREGATE)
    )
    assert both.aggregates is not None
    assert both.aggregates.income == Decimal("1000.00")
    assert both.aggregates.expense == Decimal("1.00")
    assert both.aggregates.balance == Decimal("999.00")
    assert both.total_count == 4


# --------------------------------------------------------------------------- #
# Grouping + Top N determinism
# --------------------------------------------------------------------------- #


async def test_group_by_category_deterministic_and_top_n(
    session: AsyncSession,
) -> None:
    context = await _identity(session, "ou_grp", "A")
    account = await _default_account(session, context)
    data = [
        ("餐饮", "30.00"),
        ("餐饮", "20.00"),
        ("交通", "50.00"),
        ("交通", "50.00"),
        ("居住", "10.00"),
    ]
    for index, (category, amount) in enumerate(data):
        await _entry(
            session,
            context,
            short_id=f"G{index:04d}",
            amount=amount,
            category=category,
            account_id=account.id,
        )
    await session.commit()
    service = _svc(session)
    span = {
        "start": datetime(2026, 8, 1, tzinfo=UTC),
        "end": datetime(2026, 9, 1, tzinfo=UTC),
    }
    result = await service.query(
        context,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.CATEGORY),
    )
    assert result.status is QueryStatus.OK
    assert result.groups is not None
    assert [(g.key, g.amount, g.count) for g in result.groups] == [
        ("交通", Decimal("100.00"), 2),
        ("餐饮", Decimal("50.00"), 2),
        ("居住", Decimal("10.00"), 1),
    ]
    top = await service.query(
        context,
        QueryIntent(
            **span,
            mode=QueryMode.GROUP,
            grouping=QueryGrouping.CATEGORY,
            top_n=2,
        ),
    )
    assert top.groups is not None
    assert [(g.key, g.amount) for g in top.groups] == [
        ("交通", Decimal("100.00")),
        ("餐饮", Decimal("50.00")),
    ]


async def test_group_tiebreak_by_key_asc(session: AsyncSession) -> None:
    context = await _identity(session, "ou_tiegrp", "A")
    account = await _default_account(session, context)
    for index, category in enumerate(["alice", "bob", "carol"]):
        await _entry(
            session,
            context,
            short_id=f"H{index:04d}",
            amount="10.00",
            category=category,
            account_id=account.id,
        )
    await session.commit()
    result = await _svc(session).query(
        context,
        QueryIntent(
            mode=QueryMode.GROUP,
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 9, 1, tzinfo=UTC),
            grouping=QueryGrouping.CATEGORY,
        ),
    )
    # Equal amounts → deterministic secondary ordering by key ascending.
    assert [g.key for g in result.groups] == ["alice", "bob", "carol"]


async def test_group_by_account_day_week_month(session: AsyncSession) -> None:
    context = await _identity(session, "ou_groups", "A")
    account = await _default_account(session, context)
    await _entry(
        session,
        context,
        short_id="Z0001",
        amount="10.00",
        account_id=account.id,
        occurred_at=datetime(2026, 8, 7, 17, 30, tzinfo=UTC),  # 08-08 01:30 +08
    )
    await _entry(
        session,
        context,
        short_id="Z0002",
        amount="20.00",
        account_id=account.id,
        occurred_at=datetime(2026, 8, 10, 17, 30, tzinfo=UTC),  # 08-11 01:30 +08
    )
    await session.commit()
    service = _svc(session)
    span = {
        "start": datetime(2026, 8, 1, tzinfo=UTC),
        "end": datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
    }
    day = await service.query(
        context, QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.DAY)
    )
    # ranked by amount desc → the 20.00 entry's local day first
    assert [(g.key, g.amount) for g in day.groups or []] == [
        ("2026-08-11", Decimal("20.00")),
        ("2026-08-08", Decimal("10.00")),
    ]

    week = await service.query(
        context,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.WEEK),
    )
    monday1 = (date(2026, 8, 8) - timedelta(days=date(2026, 8, 8).weekday())).isoformat()
    monday2 = (date(2026, 8, 11) - timedelta(days=date(2026, 8, 11).weekday())).isoformat()
    # Monday-based buckets; ranked by amount desc → monday2 (20.00) first.
    assert [g.key for g in week.groups or []] == [monday2, monday1]

    month = await service.query(
        context,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.MONTH),
    )
    assert [(g.key, g.amount) for g in month.groups or []] == [
        ("2026-08", Decimal("30.00"))
    ]

    account_group = await service.query(
        context,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.ACCOUNT),
    )
    assert [(g.key, g.amount) for g in account_group.groups or []] == [
        ("默认账户", Decimal("30.00"))
    ]


async def test_grouping_respects_intent_timezone(session: AsyncSession) -> None:
    """An occurrence that falls on different local days proves explicit
    timezone semantics for day bucketing."""
    context = await _identity(session, "ou_tz", "A")
    account = await _default_account(session, context)
    # 17:30 UTC = 01:30 next day in Asia/Shanghai, same day in UTC.
    await _entry(
        session,
        context,
        short_id="TZ001",
        amount="5.00",
        account_id=account.id,
        occurred_at=datetime(2026, 8, 7, 17, 30, tzinfo=UTC),
    )
    await session.commit()
    service = _svc(session)
    span = dict(
        start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 8, 31, tzinfo=UTC)
    )
    shanghai = await service.query(
        context,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.DAY),
    )
    assert [g.key for g in shanghai.groups or []] == ["2026-08-08"]

    utc = await service.query(
        context,
        QueryIntent(
            **span, timezone="UTC", mode=QueryMode.GROUP, grouping=QueryGrouping.DAY
        ),
    )
    assert [g.key for g in utc.groups or []] == ["2026-08-07"]


# --------------------------------------------------------------------------- #
# Empty / invalid / clarification / unsupported
# --------------------------------------------------------------------------- #


async def test_empty_is_controlled_result(session: AsyncSession) -> None:
    context = await _identity(session, "ou_empty", "A")
    await session.commit()
    service = _svc(session)
    empty_list = await service.query(context, QueryIntent(mode=QueryMode.LIST, page_size=10))
    assert empty_list.status is QueryStatus.EMPTY
    assert empty_list.items == []
    assert empty_list.total_count == 0

    empty_agg = await service.query(
        context,
        QueryIntent(
            mode=QueryMode.AGGREGATE,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
        ),
    )
    assert empty_agg.status is QueryStatus.EMPTY
    assert empty_agg.aggregates is None

    empty_group = await service.query(
        context,
        QueryIntent(
            mode=QueryMode.GROUP,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
            category="不存在的分类",
            grouping=QueryGrouping.CATEGORY,
        ),
    )
    assert empty_group.status is QueryStatus.EMPTY
    assert empty_group.groups is None


async def test_range_too_large_is_invalid(session: AsyncSession) -> None:
    context = await _identity(session, "ou_range", "A")
    await session.commit()
    result = await _svc(session).query(
        context,
        QueryIntent(
            mode=QueryMode.AGGREGATE,
            start=datetime(2020, 1, 1, tzinfo=UTC),
            end=datetime(2031, 1, 1, tzinfo=UTC),  # > 366 days
        ),
    )
    assert result.status is QueryStatus.INVALID
    assert result.invalid_reason is not None


async def test_unknown_account_needs_clarification(session: AsyncSession) -> None:
    context = await _identity(session, "ou_acc", "A")
    await session.commit()
    service = _svc(session)
    unknown = await service.query(
        context,
        QueryIntent(mode=QueryMode.LIST, account="不存在的账户", page_size=10),
    )
    assert unknown.status is QueryStatus.NEEDS_CLARIFICATION
    assert unknown.clarification == ["account name is unknown or ambiguous"]
    # The controlled message is identical for any non-resolvable name — the
    # planner must never act as an existence oracle.
    other = await service.query(
        context,
        QueryIntent(mode=QueryMode.LIST, account="另一个不存在的账户", page_size=10),
    )
    assert other.clarification == unknown.clarification


async def test_archived_account_still_filters_history(session: AsyncSession) -> None:
    """Query semantics cover archived accounts' history (unlike transfer
    resolution): name resolution does not require status == active."""
    context = await _identity(session, "ou_arch", "A")
    default = await _default_account(session, context)
    extra = await AccountService(session).create(
        context, name="旧卡", account_type=AccountType.CASH, currency="CNY"
    )
    await _entry(session, context, short_id="AR001", amount="1.00", account_id=default.id)
    await _entry(session, context, short_id="AR002", amount="99.00", account_id=extra.id)
    await session.commit()
    await AccountService(session).archive(context, extra.id)
    await session.commit()
    result = await _svc(session).query(
        context, QueryIntent(mode=QueryMode.LIST, account="旧卡", page_size=10)
    )
    assert result.status is QueryStatus.OK
    assert [e.short_id for e in result.items] == ["AR002"]


async def test_unsupported_grouping_is_controlled(session: AsyncSession) -> None:
    """Defense in depth: an out-of-allowlist grouping value (e.g. a stale plan
    from before the executor was extended) surfaces as a controlled
    ``unsupported``, never a result built by guessing."""
    context = await _identity(session, "ou_unsup", "A")
    await session.commit()
    # model_construct skips validation, simulating a plan whose grouping enum
    # member exists in the schema but is not implemented by the executor.
    stale = QueryPlan.model_construct(
        mode=QueryMode.GROUP,
        ledger_id=context.ledger_id,
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 9, 1, tzinfo=UTC),
        timezone=TZ,
        direction=None,
        category=None,
        account_id=None,
        account_name=None,
        min_amount=None,
        max_amount=None,
        keyword=None,
        sort=QuerySort.OCCURRED_AT,
        order=QueryOrder.DESC,
        grouping="quarter",
        top_n=None,
        page=1,
        page_size=25,
        privacy_applied=False,
    )
    result = await _svc(session).execute(context, stale)  # type: ignore[arg-type]
    assert result.status is QueryStatus.UNSUPPORTED
    assert "grouping:quarter" in result.unsupported


def test_every_declared_grouping_is_implemented() -> None:
    """You cannot add a grouping enum value without implementing it: the
    executor allowlist must cover every declared member (CI fails otherwise)."""
    from lark_ledger.services.ledger_query import _SUPPORTED_GROUPINGS

    assert set(QueryGrouping) == _SUPPORTED_GROUPINGS




# --------------------------------------------------------------------------- #
# Ledger isolation
# --------------------------------------------------------------------------- #


async def test_aggregate_income_direction_and_non_cny_currency(
    session: AsyncSession,
) -> None:
    context = await _identity(session, "ou_income", "A")
    account = await _default_account(session, context)
    await _entry(
        session,
        context,
        short_id="INC01",
        amount="800.00",
        direction=Direction.INCOME,
        account_id=account.id,
    )
    await session.commit()
    span = dict(
        start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC)
    )
    income_only = await _svc(session).query(
        context,
        QueryIntent(**span, mode=QueryMode.AGGREGATE, direction=Direction.INCOME),
    )
    assert income_only.status is QueryStatus.OK
    assert income_only.aggregates is not None
    assert income_only.aggregates.income == Decimal("800.00")
    assert "收入 ¥800.00" in income_only.message

    usd_svc = LedgerQueryService(session, timezone=TZ, currency="USD")
    usd_result = await usd_svc.query(
        context,
        QueryIntent(**span, mode=QueryMode.AGGREGATE, direction=Direction.INCOME),
    )
    assert "800.00 USD" in usd_result.message


async def test_account_name_with_control_chars_needs_clarification(
    session: AsyncSession,
) -> None:
    context = await _identity(session, "ou_ctrl", "A")
    await session.commit()
    result = await _svc(session).query(
        context, QueryIntent(mode=QueryMode.LIST, account="坏\x00名字", page_size=10)
    )
    assert result.status is QueryStatus.NEEDS_CLARIFICATION
    assert result.clarification == ["account name is invalid"]


async def test_unsupported_mode_is_controlled(session: AsyncSession) -> None:
    """Defense in depth for modes too: a stale plan with an out-of-allowlist
    mode is a controlled unsupported, never an accidental execution path."""
    context = await _identity(session, "ou_badmode", "A")
    await session.commit()
    stale = QueryPlan.model_construct(
        mode="magic",
        ledger_id=context.ledger_id,
        start=None,
        end=None,
        timezone=TZ,
        direction=None,
        category=None,
        account_id=None,
        account_name=None,
        min_amount=None,
        max_amount=None,
        keyword=None,
        sort=QuerySort.OCCURRED_AT,
        order=QueryOrder.DESC,
        grouping=None,
        top_n=None,
        page=1,
        page_size=25,
        privacy_applied=False,
    )
    result = await _svc(session).execute(context, stale)  # type: ignore[arg-type]
    assert result.status is QueryStatus.UNSUPPORTED
    assert "mode:magic" in result.unsupported


async def test_ledger_isolation_denies_other_actor(session: AsyncSession) -> None:
    ctx_a = await _identity(session, "ou_a", "A")
    ctx_b = await _identity(session, "ou_b", "B")
    await session.commit()
    # B's ledger must not be reachable with A's actor.
    foreign = RequestContext(
        actor_user_id=ctx_a.actor_user_id,
        ledger_id=ctx_b.ledger_id,
        source_channel="feishu",
        external_subject_id="ou_a",
    )
    with pytest.raises(LedgerQueryAccessDeniedError):
        await _svc(session).query(foreign, QueryIntent(mode=QueryMode.LIST, page_size=10))


# --------------------------------------------------------------------------- #
# Privacy: private accounts never leak through any channel
# --------------------------------------------------------------------------- #


async def _seed_household(
    session: AsyncSession,
    accounts: dict[str, Account],
    owner: RequestContext,
) -> None:
    await _entry(
        session,
        owner,
        short_id="S0001",
        amount="100.00",
        category="共同支出",
        account_id=accounts["shared"].id,
    )
    await _entry(
        session,
        owner,
        short_id="S0002",
        amount="500.00",
        direction=Direction.INCOME,
        category="收入",
        account_id=accounts["shared"].id,
    )
    await _entry(
        session,
        owner,
        short_id="S0003",
        amount="999.00",
        category="私人消费",
        note="私密",
        account_id=accounts["private"].id,
    )
    await _entry(
        session,
        owner,
        short_id="S0004",
        amount="50.00",
        direction=Direction.INCOME,
        category="私密收入",
        account_id=accounts["private"].id,
    )
    await session.commit()


async def test_household_private_account_no_leakage_via_any_channel(
    session: AsyncSession,
) -> None:
    owner, member, accounts = await _household(session)
    await _seed_household(session, accounts, owner)
    service = _svc(session)
    span = dict(
        start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC)
    )

    # LIST: private rows are invisible; pagination total reflects visible only.
    listing = await service.query(member, QueryIntent(**span, page_size=10))
    assert listing.status is QueryStatus.OK
    assert listing.total_count == 2
    assert {e.short_id for e in listing.items} == {"S0001", "S0002"}
    assert all(entry.account_name != "私房钱" for entry in listing.items)

    # AGGREGATE: private expense/income must not contribute.
    agg = await service.query(member, QueryIntent(**span, mode=QueryMode.AGGREGATE))
    assert agg.aggregates is not None
    assert agg.aggregates.expense == Decimal("100.00")  # not 1099.00
    assert agg.aggregates.income == Decimal("500.00")  # not 550.00
    assert agg.aggregates.count == 2

    # GROUP: private category/account never appear; amounts exclude private.
    groups = await service.query(
        member,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.CATEGORY),
    )
    assert [(g.key, g.amount) for g in groups.groups or []] == [
        ("收入", Decimal("500.00")),
        ("共同支出", Decimal("100.00")),
    ]

    acc_groups = await service.query(
        member,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.ACCOUNT),
    )
    assert [(g.key, g.amount) for g in acc_groups.groups or []] == [
        ("默认账户", Decimal("600.00"))
    ]

    # TOP N: ranking never surfaces a private amount.
    top = await service.query(
        member,
        QueryIntent(
            **span,
            mode=QueryMode.GROUP,
            grouping=QueryGrouping.CATEGORY,
            top_n=1,
            direction=Direction.EXPENSE,
        ),
    )
    assert [g.key for g in top.groups or []] == ["共同支出"]

    # Private-account name resolution must be a controlled clarification.
    secret = await service.query(member, QueryIntent(mode=QueryMode.LIST, account="私房钱"))
    assert secret.status is QueryStatus.NEEDS_CLARIFICATION


async def test_household_owner_sees_private_account(session: AsyncSession) -> None:
    owner, _member, accounts = await _household(session)
    await _seed_household(session, accounts, owner)
    span = dict(
        start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC)
    )
    agg = await _svc(session).query(owner, QueryIntent(**span, mode=QueryMode.AGGREGATE))
    assert agg.aggregates is not None
    assert agg.aggregates.expense == Decimal("1099.00")
    assert agg.aggregates.income == Decimal("550.00")
    assert agg.aggregates.count == 4


async def test_personal_ledger_privacy_is_noop(session: AsyncSession) -> None:
    context = await _identity(session, "ou_personal", "A")
    await session.commit()
    plan = await QueryPlanner(session, timezone=TZ, currency="CNY").plan(
        context, QueryIntent(mode=QueryMode.LIST, page_size=10)
    )
    assert not isinstance(plan, QueryResult)
    assert plan.privacy_applied is False
    result = await _svc(session).execute(context, plan)
    assert result.filters is not None
    assert result.filters.privacy_applied is False


# --------------------------------------------------------------------------- #
# Transfer semantics
# --------------------------------------------------------------------------- #


async def test_transfers_never_enter_entry_queries(session: AsyncSession) -> None:
    context = await _identity(session, "ou_tf", "A")
    default = await _default_account(session, context)
    second = await AccountService(session).create(
        context, name="储蓄卡", account_type=AccountType.CASH, currency="CNY"
    )
    await session.commit()
    await TransferService(session).create(
        context,
        from_account_id=default.id,
        to_account_id=second.id,
        amount=Decimal("300.00"),
        occurred_at=datetime(2026, 8, 8, 4, tzinfo=UTC),
        source_type="client",
    )
    await session.commit()
    service = _svc(session)
    span = dict(
        start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC)
    )

    listing = await service.query(context, QueryIntent(**span, page_size=10))
    assert listing.status is QueryStatus.EMPTY
    assert listing.total_count == 0

    agg = await service.query(context, QueryIntent(**span, mode=QueryMode.AGGREGATE))
    assert agg.status is QueryStatus.EMPTY
    assert agg.aggregates is None

    groups = await service.query(
        context,
        QueryIntent(**span, mode=QueryMode.GROUP, grouping=QueryGrouping.CATEGORY),
    )
    assert groups.status is QueryStatus.EMPTY
