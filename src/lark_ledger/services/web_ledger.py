"""User-scoped, bounded read models for the Web Dashboard."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    Direction,
    LedgerEntry,
    LedgerEntryRevision,
    PendingCommand,
    PendingStatus,
    User,
)
from lark_ledger.services.budget import BudgetService
from lark_ledger.services.member_resolution import MemberResolutionService
from lark_ledger.short_id import normalize_entry_ref
from lark_ledger.web_schemas import (
    CategoryValue,
    DashboardData,
    DeletedFilter,
    EntryDetail,
    EntryPage,
    EntrySort,
    SortOrder,
    TrendValue,
    WebEntry,
    WebRevision,
)


class WebLedgerQueryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        timezone: str = "Asia/Shanghai",
        currency: str = "CNY",
    ) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency

    async def list_entries(
        self,
        user_open_id: RequestContext | str,
        *,
        page: int,
        page_size: int,
        start: datetime | None = None,
        end: datetime | None = None,
        direction: Direction | None = None,
        category: str | None = None,
        source_type: str | None = None,
        amount_min: Decimal | None = None,
        amount_max: Decimal | None = None,
        search: str | None = None,
        deleted: DeletedFilter = "active",
        sort: EntrySort = "occurred_at",
        order: SortOrder = "desc",
    ) -> EntryPage:
        filters = [self._entry_scope(user_open_id)]
        privacy = await self._privacy_entry_scope(user_open_id)
        if privacy is not None:
            filters.append(privacy)
        if deleted == "active":
            filters.append(LedgerEntry.deleted_at.is_(None))
        elif deleted == "deleted":
            filters.append(LedgerEntry.deleted_at.is_not(None))
        if start is not None:
            filters.append(LedgerEntry.occurred_at >= start)
        if end is not None:
            filters.append(LedgerEntry.occurred_at < end)
        if direction is not None:
            filters.append(LedgerEntry.direction == direction)
        if category:
            filters.append(LedgerEntry.category == category)
        if source_type:
            filters.append(LedgerEntry.source_type == source_type)
        if amount_min is not None:
            filters.append(LedgerEntry.amount >= amount_min)
        if amount_max is not None:
            filters.append(LedgerEntry.amount <= amount_max)
        if search:
            term = search.strip()
            if term:
                filters.append(
                    or_(
                        LedgerEntry.note.icontains(term, autoescape=True),
                        LedgerEntry.category.icontains(term, autoescape=True),
                        LedgerEntry.short_id.icontains(term, autoescape=True),
                    )
                )
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(LedgerEntry).where(*filters)
            )
            or 0
        )
        sort_column = {
            "occurred_at": LedgerEntry.occurred_at,
            "amount": LedgerEntry.amount,
            "updated_at": LedgerEntry.updated_at,
        }[sort]
        ordering = sort_column.asc() if order == "asc" else sort_column.desc()
        rows = (
            (
                await self._session.scalars(
                    select(LedgerEntry)
                    .where(*filters)
                    .order_by(ordering, LedgerEntry.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .all()
        )
        names = await self._account_names({row.account_id for row in rows})
        payer_names = await self._payer_names(
            user_open_id, {row.paid_by_user_id for row in rows}
        )
        return EntryPage(
            items=[
                self._entry(
                    row,
                    names.get(row.account_id),
                    payer_names.get(row.paid_by_user_id),
                )
                for row in rows
            ],
            page=page,
            page_size=page_size,
            total=total,
            pages=ceil(total / page_size) if total else 0,
        )

    async def entry_detail(
        self, user_open_id: RequestContext | str, short_id: str
    ) -> EntryDetail | None:
        code = normalize_entry_ref(short_id)
        filters = [self._entry_scope(user_open_id), LedgerEntry.short_id == code]
        privacy = await self._privacy_entry_scope(user_open_id)
        if privacy is not None:
            filters.append(privacy)
        entry = await self._session.scalar(select(LedgerEntry).where(*filters))
        if entry is None:
            return None
        revisions = (
            (
                await self._session.scalars(
                    select(LedgerEntryRevision)
                    .where(
                        LedgerEntryRevision.entry_id == entry.id,
                        self._revision_scope(user_open_id),
                    )
                    .order_by(LedgerEntryRevision.created_at.desc())
                    .limit(100)
                )
            )
            .all()
        )
        names = await self._account_names({entry.account_id})
        payer_names = await self._payer_names(
            user_open_id, {entry.paid_by_user_id}
        )
        return EntryDetail(
            entry=self._entry(
                entry,
                names.get(entry.account_id),
                payer_names.get(entry.paid_by_user_id),
            ),
            revisions=[
                WebRevision(
                    id=str(row.id),
                    change_type=row.change_type,
                    before=row.before_json,
                    after=row.after_json,
                    created_at=row.created_at,
                )
                for row in revisions
            ],
        )

    async def dashboard(
        self, user_open_id: RequestContext | str, *, now: datetime | None = None
    ) -> DashboardData:
        current = (now or datetime.now(UTC)).astimezone(self._timezone)
        month_start_local = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days = monthrange(current.year, current.month)[1]
        month_end_local = month_start_local + timedelta(days=days)
        month_start = month_start_local.astimezone(UTC)
        month_end = month_end_local.astimezone(UTC)
        active = and_(
            self._entry_scope(user_open_id),
            LedgerEntry.deleted_at.is_(None),
        )
        privacy = await self._privacy_entry_scope(user_open_id)
        if privacy is not None:
            active = and_(active, privacy)
        totals = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.INCOME, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.EXPENSE, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).where(
                    active,
                    LedgerEntry.occurred_at >= month_start,
                    LedgerEntry.occurred_at < month_end,
                )
            )
        ).one()
        recent = (
            (
                await self._session.scalars(
                    select(LedgerEntry)
                    .where(active)
                    .order_by(LedgerEntry.occurred_at.desc(), LedgerEntry.id.desc())
                    .limit(10)
                )
            )
            .all()
        )
        recent_names = await self._account_names({row.account_id for row in recent})
        recent_payer_names = await self._payer_names(
            user_open_id, {row.paid_by_user_id for row in recent}
        )
        pending_filters = [
            self._pending_scope(user_open_id),
            PendingCommand.status == PendingStatus.PENDING.value,
            PendingCommand.expires_at > current.astimezone(UTC),
        ]
        if isinstance(user_open_id, RequestContext):
            from lark_ledger.services.privacy import PrivacyService

            pending_privacy = await PrivacyService(self._session).pending_visibility_scope(
                user_open_id
            )
            if pending_privacy is not None:
                pending_filters.append(pending_privacy)
        pending_count = int(
            await self._session.scalar(
                select(func.count()).select_from(PendingCommand).where(*pending_filters)
            )
            or 0
        )
        trend_start = (
            (current - timedelta(days=29))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
        )
        day_expr = func.date(LedgerEntry.occurred_at)
        trend_rows = (
            await self._session.execute(
                select(
                    day_expr,
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.INCOME, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == Direction.EXPENSE, LedgerEntry.amount),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                )
                .where(active, LedgerEntry.occurred_at >= trend_start)
                .group_by(day_expr)
                .order_by(day_expr)
            )
        ).all()
        trend_map = {row[0]: (Decimal(row[1]), Decimal(row[2])) for row in trend_rows}
        trend: list[TrendValue] = []
        for offset in range(30):
            period = (current.date() - timedelta(days=29 - offset))
            income, expense = trend_map.get(period, (Decimal(0), Decimal(0)))
            trend.append(
                TrendValue(
                    period=period,
                    income=income,
                    expense=expense,
                    balance=income - expense,
                )
            )
        category_rows = (
            await self._session.execute(
                select(LedgerEntry.category, func.sum(LedgerEntry.amount).label("amount"))
                .where(
                    active,
                    LedgerEntry.direction == Direction.EXPENSE,
                    LedgerEntry.occurred_at >= month_start,
                    LedgerEntry.occurred_at < month_end,
                )
                .group_by(LedgerEntry.category)
                .order_by(func.sum(LedgerEntry.amount).desc())
                .limit(8)
            )
        ).all()
        expense = Decimal(totals[1])
        budget_overview = await BudgetService(
            self._session, currency=self._currency, timezone=str(self._timezone)
        ).overview(user_open_id, period=month_start_local.date(), now=now)
        categories = [
            CategoryValue(
                category=str(row[0]),
                amount=Decimal(row[1]),
                ratio=(Decimal(row[1]) / expense * 100 if expense else Decimal(0)),
            )
            for row in category_rows
        ]
        income = Decimal(totals[0])
        return DashboardData(
            month_income=income,
            month_expense=expense,
            month_balance=income-expense,
            budget_usage_rate=budget_overview.usage_rate,
            pending_count=pending_count,
            recent_entries=[
                self._entry(
                    row,
                    recent_names.get(row.account_id),
                    recent_payer_names.get(row.paid_by_user_id),
                )
                for row in recent
            ],
            trend=trend,
            categories=categories,
        )

    @staticmethod
    def _legacy_subject(scope: RequestContext | str) -> str | None:
        return scope if isinstance(scope, str) else scope.external_subject_id

    @classmethod
    def _entry_scope(cls, scope: RequestContext | str) -> Any:
        if isinstance(scope, str):
            return LedgerEntry.user_open_id == scope
        legacy = cls._legacy_subject(scope)
        if legacy is None:
            return LedgerEntry.ledger_id == scope.ledger_id
        return or_(
            LedgerEntry.ledger_id == scope.ledger_id,
            and_(LedgerEntry.ledger_id.is_(None), LedgerEntry.user_open_id == legacy),
        )

    @classmethod
    def _revision_scope(cls, scope: RequestContext | str) -> Any:
        if isinstance(scope, str):
            return LedgerEntryRevision.user_open_id == scope
        legacy = cls._legacy_subject(scope)
        if legacy is None:
            return LedgerEntryRevision.ledger_id == scope.ledger_id
        return or_(
            LedgerEntryRevision.ledger_id == scope.ledger_id,
            and_(
                LedgerEntryRevision.ledger_id.is_(None),
                LedgerEntryRevision.user_open_id == legacy,
            ),
        )

    @classmethod
    def _pending_scope(cls, scope: RequestContext | str) -> Any:
        if isinstance(scope, str):
            return PendingCommand.user_open_id == scope
        # P30: recurring pendings in the ledger are household business.
        recurring_in_ledger = and_(
            PendingCommand.recurring_rule_id.is_not(None),
            PendingCommand.ledger_id == scope.ledger_id,
        )
        legacy = cls._legacy_subject(scope)
        if legacy is None:
            return or_(
                and_(
                    PendingCommand.actor_user_id == scope.actor_user_id,
                    PendingCommand.ledger_id == scope.ledger_id,
                ),
                recurring_in_ledger,
            )
        return or_(
            and_(
                PendingCommand.actor_user_id == scope.actor_user_id,
                PendingCommand.ledger_id == scope.ledger_id,
            ),
            recurring_in_ledger,
            and_(
                PendingCommand.actor_user_id.is_(None),
                PendingCommand.ledger_id.is_(None),
                PendingCommand.user_open_id == legacy,
            ),
        )

    async def _privacy_entry_scope(self, scope: RequestContext | str) -> Any | None:
        """P32: privacy filter for entry queries, ``None`` when not applicable."""
        if not isinstance(scope, RequestContext):
            return None
        from lark_ledger.services.privacy import PrivacyService

        return await PrivacyService(self._session).entry_visibility_scope(scope)

    @staticmethod
    def _entry(
        row: LedgerEntry,
        account_name: str | None,
        payer_name: str | None = None,
    ) -> WebEntry:
        return WebEntry(
            id=str(row.id),
            short_id=row.short_id,
            amount=row.amount,
            currency=row.currency,
            direction=row.direction,
            category=row.category,
            note=row.note,
            occurred_at=row.occurred_at,
            source_type=row.source_type,
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at,
            account_id=str(row.account_id) if row.account_id is not None else "",
            account_name=account_name,
            payer_user_id=(
                str(row.paid_by_user_id) if row.paid_by_user_id is not None else ""
            ),
            payer_name=payer_name,
        )

    async def _payer_names(
        self,
        scope: RequestContext | str,
        payer_ids: set[Any],
    ) -> dict[Any, str | None]:
        """Resolve payer display names (alias > display_name) in one pass."""
        ids = {payer_id for payer_id in payer_ids if payer_id is not None}
        if not ids:
            return {}
        if not isinstance(scope, RequestContext):
            rows = (
                await self._session.execute(
                    select(User.id, User.display_name).where(User.id.in_(ids))
                )
            ).all()
            return {user_id: (name or None) for user_id, name in rows}
        return await MemberResolutionService(self._session).member_display_names(scope, ids)

    async def _account_names(self, account_ids: set[Any]) -> dict[Any, str]:
        """Resolve account display names scoped to the requested ids only."""
        names: dict[Any, str] = {}
        ids = {account_id for account_id in account_ids if account_id is not None}
        if not ids:
            return names
        rows = (
            await self._session.execute(
                select(Account.id, Account.name).where(Account.id.in_(ids))
            )
        ).all()
        for account_id, name in rows:
            names[account_id] = name
        return names
