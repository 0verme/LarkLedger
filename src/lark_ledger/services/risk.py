"""Risk routing for high-risk writes (P07).

``risky_only`` policy: simple, unambiguous single-entry text writes go straight
to the ledger; image / voice / batch / likely-duplicate writes first create a
``pending_commands`` row and wait for the user's ``确认 #C-XXXXX``. Read /
query / short-ID mutation commands (list, get, update, delete, restore, export,
report, budgets) are never routed to confirmation, so v0.2.x behavior is
unchanged for them.

The router only decides. It never writes; the caller (``MessageProcessor``)
creates the pending row + preview outbox when the decision is ``PENDING``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.models import Direction, LedgerEntry
from lark_ledger.schemas import Action, EntryCandidate, ParsedCommand
from lark_ledger.services.identity import IdentityService


class RiskDecision(StrEnum):
    WRITE_THROUGH = "write_through"
    PENDING = "pending"
    REJECT = "reject"


class RiskReason(StrEnum):
    VISION = "vision"
    TRANSCRIPTION = "transcription"
    BATCH = "batch"
    CREATE_ENTRIES = "create_entries"
    BUDGETS = "budgets"
    DUPLICATE = "duplicate"
    TRANSFER = "transfer"
    RECURRING = "recurring"


class MediaKind(StrEnum):
    """How the interpretation was derived (set by the processor)."""

    NONE = "none"  # plain text
    VISION = "vision"  # image / post-with-images
    TRANSCRIPTION = "transcription"  # voice


@dataclass(frozen=True)
class DuplicateHit:
    """An existing ledger entry a candidate likely duplicates."""

    entry_index: int | None  # batch item index; None for a single command
    existing_short_id: str


@dataclass(frozen=True)
class RiskAssessment:
    decision: RiskDecision
    reason: RiskReason | None = None
    message: str | None = None
    duplicate_hits: list[DuplicateHit] = field(default_factory=list)


#: Actions that create ledger entries / budgets and may be routed to PENDING.
_WRITE_ACTIONS = frozenset(
    {
        Action.CREATE,
        Action.CREATE_ENTRIES,
        Action.BATCH,
        Action.SET_BUDGET,
        Action.SET_BUDGETS,
        Action.TRANSFER,
    }
)
#: Batch / multi-entry semantics: always confirmed when reached (media or text).
_MULTI_ENTRY_ACTIONS = frozenset({Action.CREATE_ENTRIES, Action.BATCH, Action.SET_BUDGETS})
_BATCH_REASON_BY_ACTION = {
    Action.BATCH: RiskReason.BATCH,
    Action.CREATE_ENTRIES: RiskReason.CREATE_ENTRIES,
    Action.SET_BUDGETS: RiskReason.BUDGETS,
}

_NOTE_SHINGLE_LEN = 2
_NOTE_SIMILARITY_THRESHOLD = 0.5


def _note_shingles(note: str) -> set[str]:
    """2-character shingles of a note for a small, explainable similarity check."""
    cleaned = "".join(ch for ch in (note or "").lower() if not ch.isspace())
    if len(cleaned) < _NOTE_SHINGLE_LEN:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + _NOTE_SHINGLE_LEN] for i in range(len(cleaned) - _NOTE_SHINGLE_LEN + 1)}


def note_similarity(a: str, b: str) -> float:
    """Max of Jaccard and containment over 2-character shingles (0.0 - 1.0).

    Jaccard alone is too strict for short Chinese notes ("午饭" vs "午饭外卖"
    scores 0.25); containment (shared shingles / shorter note's shingle count)
    makes a short note contained in a longer one score 1.0, which is the
    duplicate pattern that matters here.
    """
    sa = _note_shingles(a)
    sb = _note_shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = sa | sb
    return max(inter / len(union), inter / min(len(sa), len(sb)))


class RiskRouter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        policy: str = "risky_only",
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._policy = policy

    async def route(
        self,
        *,
        command: ParsedCommand,
        source_type: str,
        user_open_id: str,
        media: MediaKind = MediaKind.NONE,
        context: RequestContext | None = None,
        session: AsyncSession | None = None,
    ) -> RiskAssessment:
        """Decide write-through vs pending for a frozen command.

        ``media`` tells how the command was derived (vision / transcription /
        none); ``source_type`` is the stored event source type and is used as a
        fallback signal. Never rejects in ``risky_only`` — rejection stays with
        the existing schema / interpretation error channels.

        ``context`` is the already-resolved channel-neutral RequestContext. When
        omitted (the Feishu adapter legacy path) the router bootstraps the
        Feishu identity exactly as before; adapters that already resolved an
        identity (e.g. the Web AI entry) must pass it so no duplicate identity
        resolution happens and no wrong channel is assumed (P39 §17).
        """
        if self._policy != "risky_only":
            return RiskAssessment(decision=RiskDecision.WRITE_THROUGH)
        if command.action not in _WRITE_ACTIONS:
            return RiskAssessment(decision=RiskDecision.WRITE_THROUGH)

        if context is None:
            async with self._factory() as bootstrap_session:
                context = await IdentityService(
                    bootstrap_session,
                    currency=self._settings.currency,
                    timezone=self._settings.timezone,
                ).resolve_or_bootstrap(
                    channel="feishu",
                    external_subject_id=user_open_id,
                )
                await bootstrap_session.commit()
        hits = await self._find_duplicate_hits(
            context=context,
            user_open_id=user_open_id,
            command=command,
            source_type=source_type,
            session=session,
        )

        if command.action is Action.TRANSFER:
            return RiskAssessment(
                decision=RiskDecision.PENDING,
                reason=RiskReason.TRANSFER,
            )

        if media is MediaKind.VISION or source_type in {"image", "post"}:
            if media is MediaKind.TRANSCRIPTION or source_type == "audio":
                return RiskAssessment(
                    decision=RiskDecision.PENDING,
                    reason=RiskReason.TRANSCRIPTION,
                    duplicate_hits=hits,
                )
            return RiskAssessment(
                decision=RiskDecision.PENDING,
                reason=RiskReason.VISION,
                duplicate_hits=hits,
            )
        if media is MediaKind.TRANSCRIPTION or source_type == "audio":
            return RiskAssessment(
                decision=RiskDecision.PENDING,
                reason=RiskReason.TRANSCRIPTION,
                duplicate_hits=hits,
            )

        if command.action in _MULTI_ENTRY_ACTIONS or (
            command.batch_truncated or command.budgets_truncated
        ):
            return RiskAssessment(
                decision=RiskDecision.PENDING,
                reason=_BATCH_REASON_BY_ACTION.get(command.action, RiskReason.BATCH),
                duplicate_hits=hits,
            )

        if command.action is Action.CREATE and hits:
            return RiskAssessment(
                decision=RiskDecision.PENDING,
                reason=RiskReason.DUPLICATE,
                duplicate_hits=hits,
            )

        return RiskAssessment(decision=RiskDecision.WRITE_THROUGH)

    async def _find_duplicate_hits(
        self,
        *,
        context: RequestContext,
        user_open_id: str,
        command: ParsedCommand,
        source_type: str = "text",
        session: AsyncSession | None = None,
    ) -> list[DuplicateHit]:
        """Locate existing entries each candidate likely duplicates."""
        candidates = self._candidates(command)
        hits: list[DuplicateHit] = []
        for index, candidate in enumerate(candidates):
            # A single CREATE has no batch index; batch entries carry theirs.
            entry_index = None if command.action is Action.CREATE else index
            hits.extend(
                await self._find_duplicates(
                    context=context,
                    user_open_id=user_open_id,
                    entry_index=entry_index,
                    amount=candidate.amount,
                    currency=candidate.currency,
                    direction=candidate.direction,
                    category=candidate.category,
                    note=candidate.note or "",
                    occurred_at=candidate.occurred_at,
                    source_type=source_type,
                    session=session,
                )
            )
        return hits

    @staticmethod
    def _candidates(command: ParsedCommand) -> list[EntryCandidate]:
        if command.action is Action.CREATE:
            return [
                EntryCandidate(
                    amount=command.amount,
                    currency=command.currency,
                    direction=command.direction,
                    category=command.category,
                    note=command.note,
                    occurred_at=command.occurred_at,
                )
            ]
        if command.entries:
            return list(command.entries)
        return []

    async def _find_duplicates(
        self,
        *,
        context: RequestContext,
        user_open_id: str,
        entry_index: int | None,
        amount: Decimal | str | None,
        currency: str | None,
        direction: Direction | str | None,
        category: str | None,
        note: str,
        occurred_at: datetime | str | None,
        source_type: str,
        session: AsyncSession | None = None,
    ) -> list[DuplicateHit]:
        if amount is None or direction is None or occurred_at is None:
            return []
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)
        if isinstance(amount, str):
            amount = Decimal(amount)
        window = timedelta(minutes=self._settings.pending_duplicate_window_minutes)
        resolved_currency = currency or self._settings.currency
        resolved_direction = direction if isinstance(direction, Direction) else Direction(direction)
        # Candidate matches an existing row on the same category OR the same
        # source type, then the Python note-similarity check does the final call.
        category_conditions = []
        if category:
            category_conditions.append(LedgerEntry.category == category)
        category_conditions.append(LedgerEntry.source_type == source_type)

        async def _query() -> list[tuple[str, str | None]]:
            if session is not None:
                result = await session.execute(
                    select(LedgerEntry.short_id, LedgerEntry.note)
                    .where(
                        or_(
                            LedgerEntry.ledger_id == context.ledger_id,
                            and_(
                                LedgerEntry.ledger_id.is_(None),
                                LedgerEntry.user_open_id == user_open_id,
                            ),
                        ),
                        LedgerEntry.deleted_at.is_(None),
                        LedgerEntry.direction == resolved_direction,
                        LedgerEntry.amount == amount,
                        LedgerEntry.currency == resolved_currency,
                        LedgerEntry.occurred_at >= occurred_at - window,
                        LedgerEntry.occurred_at <= occurred_at + window,
                        or_(*category_conditions),
                    )
                    .order_by(LedgerEntry.occurred_at.desc())
                    .limit(3)
                )
                return [(short_id, note) for short_id, note in result.all()]
            async with self._factory() as factory_session:
                result = await factory_session.execute(
                    select(LedgerEntry.short_id, LedgerEntry.note)
                    .where(
                        or_(
                            LedgerEntry.ledger_id == context.ledger_id,
                            and_(
                                LedgerEntry.ledger_id.is_(None),
                                LedgerEntry.user_open_id == user_open_id,
                            ),
                        ),
                        LedgerEntry.deleted_at.is_(None),
                        LedgerEntry.direction == resolved_direction,
                        LedgerEntry.amount == amount,
                        LedgerEntry.currency == resolved_currency,
                        LedgerEntry.occurred_at >= occurred_at - window,
                        LedgerEntry.occurred_at <= occurred_at + window,
                        or_(*category_conditions),
                    )
                    .order_by(LedgerEntry.occurred_at.desc())
                    .limit(3)
                )
                return [(short_id, note) for short_id, note in result.all()]

        rows = await _query()

        hits: list[DuplicateHit] = []
        for short_id, existing_note in rows:
            if note_similarity(note, existing_note or "") >= _NOTE_SIMILARITY_THRESHOLD:
                hits.append(DuplicateHit(entry_index=entry_index, existing_short_id=short_id))
        return hits
