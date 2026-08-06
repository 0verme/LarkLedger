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
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
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
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transport: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
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
