"""P29 recurring worker + pending confirmation tests.

SQLite in-memory mirrors the worker's idempotent generation: a due rule
produces exactly one ``RecurringOccurrence`` + one confirmation ``PendingCommand``
+ one reminder outbox row, re-running and concurrent stores never duplicate, and
confirming creates exactly one ledger entry (duplicate confirms are no-ops).
PostgreSQL integration tests in ``tests/integration/test_recurring_postgres.py``
exercise the real ``FOR UPDATE SKIP LOCKED`` concurrency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import (
    AccountType,
    Base,
    Direction,
    LedgerEntry,
    PendingCommand,
    RecurringFrequency,
    RecurringOccurrence,
    RecurringOccurrenceStatus,
    RecurringRule,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.services.recurring import RecurringService, local_business_date
from lark_ledger.services.recurring_worker import RecurringWorkerStore

T0 = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
T0_LOCAL_DATE = local_business_date("Asia/Shanghai", T0)  # 2026-08-09


async def _factory() -> tuple[create_async_engine, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_rule(
    factory: async_sessionmaker,
    *,
    amount: str = "3500",
    category: str = "房租",
    description: str = "房租",
    frequency: str = "monthly",
    days_ahead: int = 0,
    status: str = "active",
) -> tuple[RecurringRule, str]:
    """Create a rule (via the service) and return it plus the feishu open_id."""
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_recur_w")
        await session.commit()
        account = await AccountService(session).get_default(context)
        # Anchor the schedule to the worker's reference date (T0), not the real
        # wall clock, so the worker scan and the rule stay in the same "today".
        next_occurrence = T0_LOCAL_DATE + timedelta(days=days_ahead)
        rule = await RecurringService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).create(
            context,
            transaction_type=Direction.EXPENSE,
            amount=Decimal(amount),
            currency=None,
            category=category,
            description=description,
            frequency=RecurringFrequency(frequency),
            interval=1,
            next_occurrence=next_occurrence,
            account_id=account.id,
            now=T0,
        )
        if status != "active":
            rule.status = status
        await session.commit()
        return rule, "ou_recur_w"


async def _count(factory: async_sessionmaker, model: type) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


async def test_worker_generates_one_pending_occurrence_and_reminder() -> None:
    engine, factory = await _factory()
    rule, _open_id = await _seed_rule(factory, days_ahead=0)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))

    generated, rows = await store.claim_and_generate("worker-1", T0, 10)

    assert len(generated) == 1
    assert generated[0].rule_id == rule.id
    assert generated[0].confirmation_code.startswith("C")
    assert len(rows) == 1  # proactive reminder card outbox row
    assert rows[0].reply_type == "direct_card"
    assert rows[0].payload_json["open_id"] == "ou_recur_w"

    assert await _count(factory, RecurringOccurrence) == 1
    assert await _count(factory, PendingCommand) == 1
    async with factory() as session:
        pending = (await session.scalars(select(PendingCommand))).one()
        assert pending.recurring_rule_id == rule.id
        assert pending.occurrence_date == rule.next_occurrence
        assert pending.ledger_id == rule.ledger_id
        assert pending.account_id == rule.account_id
        # The frozen payload carries the rule's planned date.
        assert pending.payload_json["category"] == "房租"
        assert pending.payload_json["amount"] == "3500.00"
        occurrence = (await session.scalars(select(RecurringOccurrence))).one()
        assert occurrence.status == RecurringOccurrenceStatus.PENDING.value
        assert occurrence.pending_id == pending.id
        # The schedule advanced past the generated period.
        refreshed = await session.get(RecurringRule, rule.id)
        assert refreshed.next_occurrence > rule.next_occurrence
    await engine.dispose()


async def test_worker_rerun_is_idempotent() -> None:
    engine, factory = await _factory()
    await _seed_rule(factory, days_ahead=0)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))

    first, _rows = await store.claim_and_generate("worker-1", T0, 10)
    second, second_rows = await store.claim_and_generate("worker-1", T0 + timedelta(seconds=1), 10)
    third, third_rows = await store.claim_and_generate("worker-2", T0 + timedelta(seconds=2), 10)

    assert len(first) == 1
    assert len(second) == 0
    assert len(third) == 0
    assert second_rows == []
    assert third_rows == []
    assert await _count(factory, RecurringOccurrence) == 1
    assert await _count(factory, PendingCommand) == 1
    await engine.dispose()


async def test_worker_generates_up_to_batch_size_rules() -> None:
    engine, factory = await _factory()
    for index in range(3):
        await _seed_rule(factory, days_ahead=0, amount=str(100 + index), category=f"类{index}")
    store = RecurringWorkerStore(factory, Settings(_env_file=None))

    generated, _rows = await store.claim_and_generate("worker-1", T0, 2)

    assert len(generated) == 2
    assert await _count(factory, PendingCommand) == 2
    await engine.dispose()


async def test_worker_does_not_generate_for_future_rule() -> None:
    engine, factory = await _factory()
    await _seed_rule(factory, days_ahead=10)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))

    generated, rows = await store.claim_and_generate("worker-1", T0, 10)

    assert generated == []
    assert rows == []
    assert await _count(factory, PendingCommand) == 0
    await engine.dispose()


async def test_worker_does_not_generate_for_paused_or_disabled_rule() -> None:
    engine, factory = await _factory()
    await _seed_rule(factory, days_ahead=0, status="paused")
    await _seed_rule(factory, days_ahead=0, category="停用", description="停用", status="disabled")
    store = RecurringWorkerStore(factory, Settings(_env_file=None))

    generated, rows = await store.claim_and_generate("worker-1", T0, 10)

    assert generated == []
    assert rows == []
    await engine.dispose()


async def test_skip_prevents_worker_generation() -> None:
    engine, factory = await _factory()
    rule, _ = await _seed_rule(factory, days_ahead=0)
    # User skips the current period before the worker runs.
    async with factory() as session:
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_recur_w")
        await session.commit()
        await RecurringService(session, currency="CNY", timezone="Asia/Shanghai").skip_occurrence(
            context, rule.id
        )
        await session.commit()

    store = RecurringWorkerStore(factory, Settings(_env_file=None))
    generated, rows = await store.claim_and_generate("worker-1", T0, 10)

    assert generated == []
    assert rows == []
    assert await _count(factory, PendingCommand) == 0
    async with factory() as session:
        occurrences = (await session.scalars(select(RecurringOccurrence))).all()
        assert len(occurrences) == 1
        assert occurrences[0].status == RecurringOccurrenceStatus.SKIPPED.value
    await engine.dispose()


# -- confirmation ----------------------------------------------------------

async def test_confirm_creates_one_entry_and_confirms_occurrence() -> None:
    engine, factory = await _factory()
    rule, _ = await _seed_rule(factory, days_ahead=0)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))
    await store.claim_and_generate("worker-1", T0, 10)

    async with factory() as session:
        code = (await session.scalars(select(PendingCommand))).one().confirmation_code

    pending_store = PendingCommandStore(factory, Settings(_env_file=None))
    message, rows = await pending_store.confirm_and_execute(
        user_open_id="ou_recur_w",
        confirmation_code=code,
        reply_to_message_id="om_confirm",
        confirm_event_id=None,
        exchange_rates=None,
        now=T0 + timedelta(hours=1),
    )

    assert "已记录" in message
    assert len(rows) == 1
    assert await _count(factory, LedgerEntry) == 1
    async with factory() as session:
        entry = (await session.scalars(select(LedgerEntry))).one()
        assert entry.category == "房租"
        assert entry.amount == Decimal("3500")
        assert entry.direction is Direction.EXPENSE
        assert entry.account_id == rule.account_id
        assert entry.ledger_id == rule.ledger_id
        occurrence = (await session.scalars(select(RecurringOccurrence))).one()
        assert occurrence.status == RecurringOccurrenceStatus.CONFIRMED.value
        assert occurrence.entry_id == entry.id
    await engine.dispose()


async def test_duplicate_confirm_still_creates_one_entry() -> None:
    engine, factory = await _factory()
    await _seed_rule(factory, days_ahead=0)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))
    await store.claim_and_generate("worker-1", T0, 10)

    async with factory() as session:
        code = (await session.scalars(select(PendingCommand))).one().confirmation_code

    pending_store = PendingCommandStore(factory, Settings(_env_file=None))
    now = T0 + timedelta(hours=1)
    first_message, _ = await pending_store.confirm_and_execute(
        user_open_id="ou_recur_w",
        confirmation_code=code,
        reply_to_message_id="om_confirm",
        confirm_event_id=None,
        exchange_rates=None,
        now=now,
    )
    second_message, _ = await pending_store.confirm_and_execute(
        user_open_id="ou_recur_w",
        confirmation_code=code,
        reply_to_message_id="om_confirm2",
        confirm_event_id=None,
        exchange_rates=None,
        now=now + timedelta(seconds=1),
    )

    assert "已记录" in first_message
    assert "已确认并已入账" in second_message
    assert await _count(factory, LedgerEntry) == 1
    await engine.dispose()


async def test_cancel_pending_marks_occurrence_cancelled() -> None:
    engine, factory = await _factory()
    await _seed_rule(factory, days_ahead=0)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))
    await store.claim_and_generate("worker-1", T0, 10)

    async with factory() as session:
        code = (await session.scalars(select(PendingCommand))).one().confirmation_code

    pending_store = PendingCommandStore(factory, Settings(_env_file=None))
    message, _ = await pending_store.cancel(
        user_open_id="ou_recur_w",
        confirmation_code=code,
        reply_to_message_id="om_cancel",
        cancel_event_id=None,
        now=T0 + timedelta(hours=1),
    )

    assert "已取消" in message
    assert await _count(factory, LedgerEntry) == 0
    async with factory() as session:
        occurrence = (await session.scalars(select(RecurringOccurrence))).one()
        assert occurrence.status == RecurringOccurrenceStatus.CANCELLED.value
    await engine.dispose()


async def test_confirm_after_account_switch_uses_frozen_account() -> None:
    engine, factory = await _factory()
    rule, _ = await _seed_rule(factory, days_ahead=0)
    store = RecurringWorkerStore(factory, Settings(_env_file=None))
    await store.claim_and_generate("worker-1", T0, 10)
    frozen_account_id = rule.account_id

    async with factory() as session:
        code = (await session.scalars(select(PendingCommand))).one().confirmation_code
        context = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(channel="feishu", external_subject_id="ou_recur_w")
        # User changes the default account after the pending was generated.
        other = await AccountService(session).create(
            context,
            name="新卡",
            account_type=AccountType.CASH,
            subtype=None,
            provider=None,
            currency=None,
            opening_balance=Decimal("0"),
            make_default=True,
        )
        await session.commit()

    pending_store = PendingCommandStore(factory, Settings(_env_file=None))
    await pending_store.confirm_and_execute(
        user_open_id="ou_recur_w",
        confirmation_code=code,
        reply_to_message_id="om_confirm",
        confirm_event_id=None,
        exchange_rates=None,
        now=T0 + timedelta(hours=1),
    )

    async with factory() as session:
        entry = (await session.scalars(select(LedgerEntry))).one()
        assert entry.account_id == frozen_account_id  # frozen, not the new default
        assert entry.account_id != other.id
    await engine.dispose()
