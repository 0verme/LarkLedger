import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Direction(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_entries_user_occurred", "user_open_id", "occurred_at"),
        Index("ix_entries_user_category", "user_open_id", "category"),
        UniqueConstraint(
            "source_message_id", "source_item_index", name="uq_entries_source_item"
        ),
        UniqueConstraint("user_open_id", "short_id", name="uq_entries_user_short_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    short_id: Mapped[str] = mapped_column(String(5), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, name="entry_direction"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    source_message_id: Mapped[str | None] = mapped_column(String(128))
    source_item_index: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CategoryBudget(Base):
    __tablename__ = "category_budgets"
    __table_args__ = (
        UniqueConstraint("user_open_id", "category", name="uq_budgets_user_category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BudgetAlert(Base):
    __tablename__ = "budget_alerts"
    __table_args__ = (
        UniqueConstraint(
            "budget_id", "period_start", "threshold", name="uq_budget_alerts_period_threshold"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    budget_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("category_budgets.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    threshold: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    alerted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProcessedEvent(Base):
    """Claimed Feishu events.

    New claims store a versioned JSON payload for future workers. Historical rows
    may have ``payload_json IS NULL`` and ``status=legacy_succeeded`` and are not
    replayable.

    Reliable-delivery state (v0.2.1 / P05a + P05b): ``attempt_count``,
    ``next_attempt_at``, ``lease_owner``, ``lease_expires_at``, and
    ``result_summary`` back the event worker's retry, lease, and dead-letter
    model. The worker writes the lease / scheduling columns on claim and clears
    them on completion or failure. The legacy sync path (``worker_enabled=false``)
    writes only ``attempt_count`` and ``result_summary``. ``source_message_id`` /
    ``user_open_id`` are denormalized at claim time for operator lookups.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        Index("ix_events_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_events_lease_expires", "lease_expires_at"),
        Index("ix_events_source_message", "source_message_id"),
        Index("ix_events_user_open_id", "user_open_id"),
        Index("ix_events_cleanup_processed", "status", "processed_at"),
        Index("ix_events_cleanup_updated", "status", "updated_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transport: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    manual_replay_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    replay_safety_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EventReplayAudit(Base):
    """Append-only operator audit for guarded manual event replay.

    ``event_id`` intentionally has no foreign key: terminal event cleanup must
    not erase the replay audit. Payloads and user financial content are never
    copied into this table.
    """

    __tablename__ = "event_replay_audits"
    __table_args__ = (
        Index("ix_event_replay_audits_event_created", "event_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operator: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="replay_event")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    resulting_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReplyOutbox(Base):
    """Transactional outbox for durable Feishu reply intents (P06a).

    A row is written in the **same database transaction** as the business
    change it confirms, so "the reply intent is durable" is atomic with "the
    ledger change happened". A later worker (P06b) may pick up ``pending`` /
    ``failed`` rows and deliver them without re-executing business: every row is
    self-contained (recipient ``message_id``, reply type, JSON envelope, and —
    for files / report images — the raw ``payload_blob`` bytes plus size and
    checksum), so nothing has to be re-derived from AI, in-memory objects, or
    temporary files.

    Idempotency: ``(event_id, reply_type)`` is unique, so a retried event can
    never insert a duplicate reply. ``status`` values come from the
    ``ReplyStatus`` enum; ``sending`` and ``dead`` are reserved for the P06b
    background worker and are not produced in P06a. ``sequence`` gives a stable
    order for multi-message replies (e.g. CSV export sends its file before its
    confirmation text).
    """

    __tablename__ = "reply_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", "reply_type", name="uq_outbox_event_type"),
        Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_outbox_lease_expires", "lease_expires_at"),
        Index("ix_outbox_event_sequence", "event_id", "sequence"),
        Index("ix_outbox_cleanup_sent", "status", "sent_at"),
        Index("ix_outbox_cleanup_updated", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("processed_events.event_id", ondelete="CASCADE"),
        nullable=True,
    )
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reply_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="feishu")
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # P06b remote delivery metadata: the Feishu message_id of the delivered
    # reply and the resource keys obtained from uploads, persisted so a retry
    # reuses an already-uploaded file / image instead of uploading again.
    remote_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remote_file_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remote_image_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LedgerEntryRevision(Base):
    """Append-only audit snapshots for entry update/delete/restore."""

    __tablename__ = "ledger_entry_revisions"
    __table_args__ = (Index("ix_revisions_entry_created", "entry_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    short_id: Mapped[str] = mapped_column(String(5), nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PendingStatus(StrEnum):
    """Lifecycle of a high-risk command awaiting user confirmation (P07)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class PendingCommand(Base):
    """A frozen, high-risk command awaiting user confirmation (P07).

    Created when the risk router decides an image / voice / batch / likely-
    duplicate write must not hit the ledger until the user confirms. The
    ``payload_json`` holds the **frozen** ``ParsedCommand`` (``model_dump``), so
    confirming never re-calls AI or re-recognizes the media; ``preview_json``
    holds the frozen user preview (aggregates only, never OCR text or full
    transcripts). ``confirmation_code`` is the user-facing ``#C-A83F2`` form
    minus the ``#``/``-`` (stored as ``CA83F2``), user-unique and never reused.

    ``source_fingerprint`` is a privacy-safe SHA-256 of the ordered visual
    request. A partial unique index permits only one active row for the same
    user and exact media while allowing a deliberate resend after terminal
    status. Historical rows remain NULL because their media is not retained.

    ``source_event_id`` intentionally has no foreign key: terminal event
    cleanup must not cascade-delete a pending confirmation that a user may still
    act on. The unique constraint backs "one logical pending per source event"
    (Postgres treats NULLs as distinct, so out-of-band rows without an event are
    unaffected).
    """

    __tablename__ = "pending_commands"
    __table_args__ = (
        UniqueConstraint(
            "user_open_id", "confirmation_code", name="uq_pending_user_code"
        ),
        UniqueConstraint("source_event_id", name="uq_pending_source_event"),
        Index(
            "uq_pending_user_active_fingerprint",
            "user_open_id",
            "source_fingerprint",
            unique=True,
            postgresql_where=text(
                "source_fingerprint IS NOT NULL "
                "AND status IN ('pending', 'executing')"
            ),
            sqlite_where=text(
                "source_fingerprint IS NOT NULL "
                "AND status IN ('pending', 'executing')"
            ),
        ),
        Index("ix_pending_status_expires", "status", "expires_at"),
        Index("ix_pending_user_status", "user_open_id", "status"),
        Index("ix_pending_source_event", "source_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmation_code: Mapped[str] = mapped_column(String(6), nullable=False)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="feishu")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
