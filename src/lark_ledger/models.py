import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
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


class AccountType(StrEnum):
    CASH = "cash"
    ASSET = "asset"
    LIABILITY = "liability"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class LedgerKind(StrEnum):
    PERSONAL = "personal"
    HOUSEHOLD_SHARED = "household_shared"
    BUSINESS = "business"


class HouseholdStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class HouseholdRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class HouseholdMemberStatus(StrEnum):
    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"


class HouseholdInvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class User(Base):
    """Platform-independent person known to the ledger core."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=UserStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Ledger(Base):
    """Authorization and ownership root for one set of accounting records."""

    __tablename__ = "ledgers"
    __table_args__ = (
        Index(
            "uq_ledgers_owner_default",
            "owner_user_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
        Index("ix_ledgers_owner_created", "owner_user_id", "created_at"),
        UniqueConstraint("owner_user_id", "normalized_name", name="uq_ledgers_owner_name"),
        Index(
            "uq_ledgers_household_shared",
            "household_id",
            unique=True,
            postgresql_where=text("kind = 'household_shared'"),
            sqlite_where=text("kind = 'household_shared'"),
        ),
        CheckConstraint(
            "(kind = 'household_shared' AND household_id IS NOT NULL AND owner_user_id IS NULL "
            "AND is_default = false) OR "
            "(kind <> 'household_shared' AND household_id IS NULL AND owner_user_id IS NOT NULL)",
            name="ck_ledgers_ownership_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    household_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("households.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=LedgerKind.PERSONAL.value)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    is_default: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Household(Base):
    __tablename__ = "households"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "normalized_name", name="uq_households_owner_name"),
        Index("ix_households_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=HouseholdStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HouseholdMember(Base):
    __tablename__ = "household_members"
    __table_args__ = (
        UniqueConstraint("household_id", "user_id", name="uq_household_members_user"),
        Index(
            "uq_household_members_active_owner",
            "household_id",
            unique=True,
            postgresql_where=text("role = 'owner' AND status = 'active'"),
            sqlite_where=text("role = 'owner' AND status = 'active'"),
        ),
        Index("ix_household_members_user_status", "user_id", "status"),
        CheckConstraint("role IN ('owner', 'member')", name="ck_household_members_role"),
        CheckConstraint(
            "status IN ('active', 'left', 'removed')", name="ck_household_members_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    household_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HouseholdInvitation(Base):
    __tablename__ = "household_invitations"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_household_invitations_public_id"),
        Index(
            "uq_household_invitations_active_target",
            "household_id",
            "target_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("ix_household_invitations_target_status", "target_user_id", "status"),
        Index("ix_household_invitations_expires", "status", "expires_at"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'cancelled', 'expired')",
            name="ck_household_invitations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    household_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("households.id", ondelete="CASCADE"), nullable=False
    )
    inviter_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    target_channel_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channel_identities.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HouseholdInvitationStatus.PENDING.value
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ChannelIdentity(Base):
    """A platform subject mapped to exactly one internal user."""

    __tablename__ = "channel_identities"
    __table_args__ = (
        UniqueConstraint("channel", "external_subject_id", name="uq_channel_identity_subject"),
        Index("ix_channel_identities_user", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Account(Base):
    """Ledger-scoped place where personal or household funds are held."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("ledger_id", "id", name="uq_accounts_ledger_id"),
        UniqueConstraint("ledger_id", "normalized_name", name="uq_accounts_ledger_name"),
        Index("ix_accounts_ledger_status", "ledger_id", "status", "created_at"),
        Index(
            "uq_accounts_ledger_default",
            "ledger_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
        CheckConstraint("type IN ('cash', 'asset', 'liability')", name="ck_accounts_type"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_accounts_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AccountStatus.ACTIVE.value
    )
    is_default: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_entries_user_occurred", "user_open_id", "occurred_at"),
        Index("ix_entries_user_category", "user_open_id", "category"),
        UniqueConstraint("source_message_id", "source_item_index", name="uq_entries_source_item"),
        UniqueConstraint("ledger_id", "short_id", name="uq_entries_ledger_short_id"),
        Index("ix_entries_ledger_occurred", "ledger_id", "occurred_at"),
        Index("ix_entries_ledger_category", "ledger_id", "category"),
        ForeignKeyConstraint(
            ["ledger_id", "account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_entries_ledger_account",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=True
    )
    # Nullable in metadata so legacy fixture/data construction remains loadable;
    # migration 0019 backfills and enforces NOT NULL in deployed databases.
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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


class Transfer(Base):
    """A ledger-scoped movement of funds between two accounts.

    Transfers are deliberately separate from ``LedgerEntry`` so they can never
    be counted as income, expense, category consumption, or budget usage.
    """

    __tablename__ = "transfers"
    __table_args__ = (
        UniqueConstraint("ledger_id", "id", name="uq_transfers_ledger_id"),
        ForeignKeyConstraint(
            ["ledger_id", "from_account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_transfers_ledger_from_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "to_account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_transfers_ledger_to_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint("from_account_id <> to_account_id", name="ck_transfers_distinct_accounts"),
        CheckConstraint("amount > 0", name="ck_transfers_positive_amount"),
        Index("ix_transfers_ledger_occurred", "ledger_id", "occurred_at"),
        Index("ix_transfers_ledger_from", "ledger_id", "from_account_id", "occurred_at"),
        Index("ix_transfers_ledger_to", "ledger_id", "to_account_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=False
    )
    from_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    to_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="client")
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TransferRevision(Base):
    """Append-only audit snapshots for transfer changes and reversal."""

    __tablename__ = "transfer_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ledger_id", "transfer_id"],
            ["transfers.ledger_id", "transfers.id"],
            name="fk_transfer_revisions_ledger_transfer",
            ondelete="RESTRICT",
        ),
        Index("ix_transfer_revisions_transfer_created", "transfer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ledger_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CategoryBudget(Base):
    __tablename__ = "category_budgets"
    __table_args__ = (UniqueConstraint("ledger_id", "category", name="uq_budgets_ledger_category"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Budget(Base):
    """Ledger-scoped, period-specific monthly budget (P28 Budget 2.0).

    ``period`` is the first day of the month (e.g. ``2026-08-01``) and is an
    explicit business field, never derived from timestamps. ``category`` is
    ``NULL`` for the ledger's total budget for that period and a non-empty
    category name for a category budget.

    Period budgets layer on top of the legacy recurring ``CategoryBudget`` rows
    that predate period support: a period row wins for its month, otherwise the
    recurring budget applies to that category. Budget limits stay in this table
    while actual spending is always computed from the live ``LedgerEntry``
    facts, so delete / restore / revision never drift a cached counter.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint(
            "ledger_id", "period", "category", name="uq_budgets_ledger_period_category"
        ),
        Index(
            "uq_budgets_ledger_period_total",
            "ledger_id",
            "period",
            unique=True,
            postgresql_where=text("category IS NULL"),
            sqlite_where=text("category IS NULL"),
        ),
        CheckConstraint(
            "category IS NULL OR length(category) > 0", name="ck_budgets_category_nonempty"
        ),
        Index("ix_budgets_ledger_period", "ledger_id", "period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
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
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    __table_args__ = (Index("ix_event_replay_audits_event_created", "event_id", "created_at"),)

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
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
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
        UniqueConstraint("user_open_id", "confirmation_code", name="uq_pending_user_code"),
        UniqueConstraint("source_event_id", name="uq_pending_source_event"),
        ForeignKeyConstraint(
            ["ledger_id", "from_account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_pending_ledger_from_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "to_account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_pending_ledger_to_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "account_id"],
            ["accounts.ledger_id", "accounts.id"],
            name="fk_pending_ledger_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(account_id IS NULL AND from_account_id IS NULL AND to_account_id IS NULL "
            "AND transfer_id IS NULL) OR "
            "(account_id IS NOT NULL AND from_account_id IS NULL AND to_account_id IS NULL "
            "AND transfer_id IS NULL) OR "
            "(account_id IS NULL AND ledger_id IS NOT NULL AND from_account_id IS NOT NULL "
            "AND to_account_id IS NOT NULL AND transfer_id IS NOT NULL "
            "AND from_account_id <> to_account_id)",
            name="ck_pending_transfer_target",
        ),
        Index(
            "uq_pending_ledger_active_fingerprint",
            "ledger_id",
            "source_fingerprint",
            unique=True,
            postgresql_where=text(
                "source_fingerprint IS NOT NULL AND status IN ('pending', 'executing')"
            ),
            sqlite_where=text(
                "source_fingerprint IS NOT NULL AND status IN ('pending', 'executing')"
            ),
        ),
        Index("ix_pending_status_expires", "status", "expires_at"),
        Index("ix_pending_user_status", "user_open_id", "status"),
        Index("ix_pending_source_event", "source_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmation_code: Mapped[str] = mapped_column(String(6), nullable=False)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=True
    )
    from_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Frozen single-account target for non-transfer write pendings (create /
    # update / batch). NULL for legacy rows and for transfer / budget pendings.
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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


class DashboardSession(Base):
    """Revocable server-side session for the optional Web Dashboard."""

    __tablename__ = "dashboard_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_dashboard_sessions_token_hash"),
        Index("ix_dashboard_sessions_expires", "expires_at"),
        Index("ix_dashboard_sessions_user", "user_open_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="RESTRICT"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    avatar_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClientCredential(Base):
    """Revocable bearer credential. Only a SHA-256 digest is persisted."""

    __tablename__ = "client_credentials"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_client_credentials_token_digest"),
        Index("ix_client_credentials_user_created", "user_id", "created_at"),
        Index("ix_client_credentials_expires", "expires_at"),
        CheckConstraint("scopes <> ''", name="ck_client_credentials_scopes_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    current_ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[str] = mapped_column(String(512), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClientIdempotencyRecord(Base):
    """Durable result snapshot for one actor, operation, ledger and key."""

    __tablename__ = "client_idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "operation",
            "ledger_id",
            "idempotency_key",
            name="uq_client_idempotency_scope",
        ),
        Index("ix_client_idempotency_expires", "expires_at"),
        Index("ix_client_idempotency_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClientSecurityAudit(Base):
    """Minimal security audit without credential or financial payload material."""

    __tablename__ = "client_security_audits"
    __table_args__ = (
        Index("ix_client_security_audits_actor_created", "actor_user_id", "created_at"),
        Index("ix_client_security_audits_action_created", "action", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
