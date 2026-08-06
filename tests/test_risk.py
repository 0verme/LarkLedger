"""P07: risk router classification and duplicate detection."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction, LedgerEntry
from lark_ledger.schemas import Action, EntryCandidate, ParsedCommand
from lark_ledger.services.risk import (
    MediaKind,
    RiskDecision,
    RiskReason,
    RiskRouter,
    note_similarity,
)

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _create(**kw: Any) -> ParsedCommand:
    base: dict[str, Any] = dict(
        action=Action.CREATE,
        amount=Decimal("32.00"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=T0,
    )
    base.update(kw)
    return ParsedCommand(**base)


def _batch(entries: list[EntryCandidate]) -> ParsedCommand:
    return ParsedCommand(action=Action.BATCH, entries=entries)


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[Any]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    factory: async_sessionmaker[Any],
    *,
    amount: str = "32.00",
    category: str = "餐饮",
    note: str = "午饭",
    occurred_at: datetime = T0,
    user: str = "ou_user",
    source_type: str = "text",
    direction: str = "expense",
    short_id: str = "A83F2",
) -> None:
    async with factory() as session:
        session.add(
            LedgerEntry(
                user_open_id=user,
                short_id=short_id,
                amount=Decimal(amount),
                currency="CNY",
                direction=Direction(direction),
                category=category,
                note=note,
                occurred_at=occurred_at,
                source_type=source_type,
                source_message_id="om_orig",
                source_item_index=0,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Note similarity
# ---------------------------------------------------------------------------


def test_note_similarity() -> None:
    assert note_similarity("午饭", "午饭") == 1.0
    assert note_similarity("午饭", "午饭外卖") > 0.5
    assert note_similarity("午饭", "晚餐") == 0.0
    assert note_similarity("", "午饭") == 0.0


# ---------------------------------------------------------------------------
# Routing decisions
# ---------------------------------------------------------------------------


async def test_simple_text_create_writes_through() -> None:
    engine, factory = await _sqlite_factory()
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(), source_type="text", user_open_id="ou_user"
    )
    assert assessment.decision is RiskDecision.WRITE_THROUGH
    await engine.dispose()


async def test_vision_media_routes_to_pending() -> None:
    engine, factory = await _sqlite_factory()
    router = RiskRouter(factory, _settings())
    for source_type, media in (
        ("image", MediaKind.VISION),
        ("post", MediaKind.VISION),
        ("post", MediaKind.NONE),
    ):
        assessment = await router.route(
            command=_create(),
            source_type=source_type,
            user_open_id="ou_user",
            media=media,
        )
        assert assessment.decision is RiskDecision.PENDING, (source_type, media)
        assert assessment.reason is RiskReason.VISION
    await engine.dispose()


async def test_audio_routes_to_pending() -> None:
    engine, factory = await _sqlite_factory()
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(),
        source_type="audio",
        user_open_id="ou_user",
        media=MediaKind.TRANSCRIPTION,
    )
    assert assessment.decision is RiskDecision.PENDING
    assert assessment.reason is RiskReason.TRANSCRIPTION
    await engine.dispose()


async def test_batch_actions_route_to_pending() -> None:
    engine, factory = await _sqlite_factory()
    router = RiskRouter(factory, _settings())
    cases: list[tuple[ParsedCommand, RiskReason]] = [
        (
            ParsedCommand(
                action=Action.BATCH,
                entries=[EntryCandidate(amount="32", direction="expense")],
            ),
            RiskReason.BATCH,
        ),
        (
            ParsedCommand(
                action=Action.CREATE_ENTRIES,
                entries=[EntryCandidate(amount="32", direction="expense")],
            ),
            RiskReason.CREATE_ENTRIES,
        ),
        (
            ParsedCommand(
                action=Action.SET_BUDGETS,
                budgets=[{"category": "餐饮", "amount": "1000"}],
            ),
            RiskReason.BUDGETS,
        ),
    ]
    for command, expected_reason in cases:
        assessment = await router.route(
            command=command, source_type="text", user_open_id="ou_user"
        )
        assert assessment.decision is RiskDecision.PENDING
        assert assessment.reason is expected_reason
    await engine.dispose()


async def test_read_and_mutation_commands_never_pending() -> None:
    engine, factory = await _sqlite_factory()
    router = RiskRouter(factory, _settings())
    # Each command must pass the strict per-action schema validation.
    commands: list[tuple[Action, ParsedCommand]] = [
        (Action.LIST_ENTRIES, ParsedCommand(action=Action.LIST_ENTRIES)),
        (Action.GET_ENTRY, ParsedCommand(action=Action.GET_ENTRY, entry_ref="A83F2")),
        (
            Action.UPDATE_ENTRY,
            ParsedCommand(
                action=Action.UPDATE_ENTRY, entry_ref="A83F2", amount=Decimal("35")
            ),
        ),
        (Action.DELETE_ENTRY, ParsedCommand(action=Action.DELETE_ENTRY, entry_ref="A83F2")),
        (Action.RESTORE_ENTRY, ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref="A83F2")),
        (Action.EXPORT_ENTRIES, ParsedCommand(action=Action.EXPORT_ENTRIES)),
        (
            Action.SUMMARY,
            ParsedCommand(
                action=Action.SUMMARY,
                range_start=datetime(2026, 8, 1, tzinfo=UTC),
                range_end=datetime(2026, 8, 31, tzinfo=UTC),
            ),
        ),
        (
            Action.REPORT,
            ParsedCommand(
                action=Action.REPORT,
                range_start=datetime(2026, 8, 1, tzinfo=UTC),
                range_end=datetime(2026, 8, 31, tzinfo=UTC),
            ),
        ),
        (Action.HELP, ParsedCommand(action=Action.HELP)),
        (
            Action.UPDATE_LAST,
            ParsedCommand(action=Action.UPDATE_LAST, amount=Decimal("35")),
        ),
        (Action.UNDO_LAST, ParsedCommand(action=Action.UNDO_LAST)),
    ]
    for action, command in commands:
        # Even an image source must not confirm a read / short-ID mutation.
        assessment = await router.route(
            command=command, source_type="image", user_open_id="ou_user"
        )
        assert assessment.decision is RiskDecision.WRITE_THROUGH, action
    await engine.dispose()


async def test_duplicate_single_entry_routes_to_pending() -> None:
    engine, factory = await _sqlite_factory()
    await _seed(factory, note="午饭")
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(note="午饭"), source_type="text", user_open_id="ou_user"
    )
    assert assessment.decision is RiskDecision.PENDING
    assert assessment.reason is RiskReason.DUPLICATE
    assert [hit.existing_short_id for hit in assessment.duplicate_hits] == ["A83F2"]
    await engine.dispose()


async def test_no_duplicate_when_notes_differ() -> None:
    engine, factory = await _sqlite_factory()
    await _seed(factory, note="午饭")
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(note="生日聚餐"), source_type="text", user_open_id="ou_user"
    )
    assert assessment.decision is RiskDecision.WRITE_THROUGH
    await engine.dispose()


async def test_duplicate_respects_user_isolation() -> None:
    engine, factory = await _sqlite_factory()
    await _seed(factory, note="午饭", user="ou_other")
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(note="午饭"), source_type="text", user_open_id="ou_user"
    )
    assert assessment.decision is RiskDecision.WRITE_THROUGH
    await engine.dispose()


async def test_duplicate_respects_direction() -> None:
    engine, factory = await _sqlite_factory()
    await _seed(factory, note="午饭", direction="expense")
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(note="午饭", direction=Direction.INCOME),
        source_type="text",
        user_open_id="ou_user",
    )
    assert assessment.decision is RiskDecision.WRITE_THROUGH
    await engine.dispose()


async def test_deleted_entries_are_not_duplicates() -> None:
    engine, factory = await _sqlite_factory()
    async with factory() as session:
        row = LedgerEntry(
            user_open_id="ou_user",
            short_id="A83F2",
            amount=Decimal("32.00"),
            currency="CNY",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="午饭",
            occurred_at=T0,
            source_type="text",
            deleted_at=T0,
        )
        session.add(row)
        await session.commit()
    router = RiskRouter(factory, _settings())
    assessment = await router.route(
        command=_create(note="午饭"), source_type="text", user_open_id="ou_user"
    )
    assert assessment.decision is RiskDecision.WRITE_THROUGH
    await engine.dispose()


async def test_duplicate_hits_are_attached_to_batch_routing() -> None:
    engine, factory = await _sqlite_factory()
    await _seed(factory, note="午饭")
    router = RiskRouter(factory, _settings())
    command = _batch(
        [
            EntryCandidate(
                amount="32",
                direction="expense",
                category="餐饮",
                note="午饭",
                occurred_at=T0,
            ),
            EntryCandidate(
                amount="99",
                direction="expense",
                category="交通",
                note="打车",
                occurred_at=T0,
            ),
        ]
    )
    assessment = await router.route(
        command=command, source_type="text", user_open_id="ou_user"
    )
    assert assessment.decision is RiskDecision.PENDING
    assert assessment.reason is RiskReason.BATCH
    assert [hit.entry_index for hit in assessment.duplicate_hits] == [0]
    await engine.dispose()
