from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Direction, HouseholdInvitation, Ledger
from lark_ledger.schemas import ExecutionResult, ParsedCommand
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.household_management import (
    HouseholdManagementError,
    HouseholdManagementService,
    HouseholdMemberView,
    HouseholdView,
)
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.ledger_management import LedgerManagementService
from lark_ledger.services.web_analytics import WebAnalyticsQueryService
from lark_ledger.services.web_ledger import WebLedgerQueryService
from lark_ledger.services.web_pending import WebPendingQueryService
from lark_ledger.web_schemas import (
    AnalyticsCategory,
    AnalyticsMonthlyPoint,
    AnalyticsSummary,
    AnalyticsTrendPoint,
    BudgetOverview,
    DashboardData,
    DeletedFilter,
    EntryDetail,
    EntryPage,
    EntrySort,
    PendingDetail,
    PendingGroup,
    PendingPage,
    SortOrder,
)


@dataclass(frozen=True, slots=True)
class EntryQuery:
    page: int = 1
    page_size: int = 25
    start: datetime | None = None
    end: datetime | None = None
    direction: Direction | None = None
    category: str | None = None
    source_type: str | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    search: str | None = None
    deleted: DeletedFilter = "active"
    sort: EntrySort = "occurred_at"
    order: SortOrder = "desc"


class ClientApplicationService:
    """Transport-neutral command/query boundary for authenticated clients."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        currency: str,
        timezone: str,
        exchange_rates: ExchangeRateService | None = None,
    ) -> None:
        self._session = session
        self._currency = currency
        self._timezone = timezone
        self._exchange_rates = exchange_rates
        self._authorization = LedgerAuthorizationService(session)

    async def authorize(self, context: RequestContext) -> Ledger:
        return await self._authorization.get_accessible(
            context.actor_user_id, context.ledger_id
        )

    async def list_ledgers(self, context: RequestContext) -> list[Ledger]:
        return await self._authorization.list_accessible(context.actor_user_id)

    async def list_personal_ledgers(self, context: RequestContext) -> list[Ledger]:
        await self.authorize(context)
        return await self._ledger_manager().list_owned(context.actor_user_id)

    async def current_ledger(self, context: RequestContext) -> Ledger:
        return await self.authorize(context)

    async def current_personal_ledger(self, context: RequestContext) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().get_owned(
            context.actor_user_id, context.ledger_id
        )

    async def find_personal_ledger(
        self, context: RequestContext, name: str
    ) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().find_owned_by_name(
            context.actor_user_id, name
        )

    async def select_channel_ledger(
        self, context: RequestContext, ledger_id: uuid.UUID
    ) -> Ledger:
        await self.authorize(context)
        if context.channel_identity_id is None:
            raise ValueError("channel identity is required for persisted selection")
        return await self._ledger_manager().select_for_channel(
            context.actor_user_id, context.channel_identity_id, ledger_id
        )

    async def create_personal_ledger(self, context: RequestContext, name: str) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().create(context.actor_user_id, name)

    async def rename_personal_ledger(
        self, context: RequestContext, ledger_id: uuid.UUID, name: str
    ) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().rename(context.actor_user_id, ledger_id, name)

    async def set_default_ledger(
        self, context: RequestContext, ledger_id: uuid.UUID
    ) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().set_default(context.actor_user_id, ledger_id)

    async def execute_financial(
        self,
        context: RequestContext,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None = None,
        expected_updated_at: datetime | None = None,
        commit_changes: bool = True,
    ) -> ExecutionResult:
        await self.authorize(context)
        return await LedgerService(
            self._session,
            currency=self._currency,
            timezone=self._timezone,
            exchange_rates=self._exchange_rates,
            commit_changes=commit_changes,
        ).execute(
            context,
            command,
            source_type=source_type,
            source_message_id=source_message_id,
            expected_updated_at=expected_updated_at,
        )

    async def dashboard(self, context: RequestContext) -> DashboardData:
        await self.authorize(context)
        return await WebLedgerQueryService(
            self._session, timezone=self._timezone
        ).dashboard(context)

    async def list_entries(
        self, context: RequestContext, query: EntryQuery
    ) -> EntryPage:
        await self.authorize(context)
        return await WebLedgerQueryService(
            self._session, timezone=self._timezone
        ).list_entries(
            context,
            page=query.page,
            page_size=query.page_size,
            start=query.start,
            end=query.end,
            direction=query.direction,
            category=query.category,
            source_type=query.source_type,
            amount_min=query.amount_min,
            amount_max=query.amount_max,
            search=query.search,
            deleted=query.deleted,
            sort=query.sort,
            order=query.order,
        )

    async def entry_detail(
        self, context: RequestContext, short_id: str
    ) -> EntryDetail | None:
        await self.authorize(context)
        return await WebLedgerQueryService(
            self._session, timezone=self._timezone
        ).entry_detail(context, short_id)

    async def budgets(self, context: RequestContext) -> BudgetOverview:
        await self.authorize(context)
        return await WebAnalyticsQueryService(
            self._session, timezone=self._timezone, currency=self._currency
        ).budgets(context)

    async def analytics(
        self, context: RequestContext, *, start_date: date, end_date: date
    ) -> tuple[
        AnalyticsSummary,
        list[AnalyticsTrendPoint],
        list[AnalyticsCategory],
        list[AnalyticsMonthlyPoint],
    ]:
        await self.authorize(context)
        return await WebAnalyticsQueryService(
            self._session, timezone=self._timezone, currency=self._currency
        ).analytics(context, start_date=start_date, end_date=end_date)

    async def list_pending(
        self,
        context: RequestContext,
        *,
        group: PendingGroup,
        page: int,
        page_size: int,
    ) -> PendingPage:
        await self.authorize(context)
        return await WebPendingQueryService(self._session).list_pending(
            context, group=group, page=page, page_size=page_size
        )

    async def pending_detail(
        self, context: RequestContext, confirmation_id: str
    ) -> PendingDetail | None:
        await self.authorize(context)
        return await WebPendingQueryService(self._session).detail(context, confirmation_id)

    async def list_households(self, context: RequestContext) -> list[HouseholdView]:
        await self.authorize(context)
        return await self._household_manager().list_for_user(context.actor_user_id)

    async def get_household(
        self, context: RequestContext, household_id: uuid.UUID
    ) -> HouseholdView:
        await self.authorize(context)
        return await self._household_manager().get(context.actor_user_id, household_id)

    async def resolve_household(
        self, context: RequestContext, name: str | None = None
    ) -> HouseholdView:
        await self.authorize(context)
        manager = self._household_manager()
        if name:
            return await manager.find_by_name(context.actor_user_id, name)
        households = await manager.list_for_user(context.actor_user_id)
        current = [item for item in households if item.ledger.id == context.ledger_id]
        if len(current) == 1:
            return current[0]
        if len(households) == 1:
            return households[0]
        raise HouseholdManagementError(
            "select a household ledger first when the actor belongs to multiple households"
        )

    async def create_household(self, context: RequestContext, name: str) -> HouseholdView:
        await self.authorize(context)
        return await self._household_manager().create(context.actor_user_id, name)

    async def rename_household(
        self, context: RequestContext, household_id: uuid.UUID, name: str
    ) -> HouseholdView:
        await self.authorize(context)
        return await self._household_manager().rename(
            context.actor_user_id, household_id, name
        )

    async def list_household_members(
        self, context: RequestContext, household_id: uuid.UUID
    ) -> list[HouseholdMemberView]:
        await self.authorize(context)
        return await self._household_manager().list_members(
            context.actor_user_id, household_id
        )

    async def invite_household_member(
        self, context: RequestContext, household_id: uuid.UUID, target: str
    ) -> HouseholdInvitation:
        await self.authorize(context)
        return await self._household_manager().invite(
            context.actor_user_id, household_id, target
        )

    async def list_household_invitations(
        self, context: RequestContext
    ) -> list[HouseholdInvitation]:
        await self.authorize(context)
        return await self._household_manager().list_invitations(context.actor_user_id)

    async def respond_household_invitation(
        self, context: RequestContext, invitation_id: uuid.UUID | str, action: str
    ) -> HouseholdInvitation:
        await self.authorize(context)
        manager = self._household_manager()
        if action == "accept":
            return await manager.accept(context.actor_user_id, invitation_id)
        if action == "reject":
            return await manager.reject(context.actor_user_id, invitation_id)
        if action == "cancel":
            return await manager.cancel_invitation(context.actor_user_id, invitation_id)
        raise ValueError("unsupported invitation action")

    async def leave_household(
        self, context: RequestContext, household_id: uuid.UUID
    ) -> None:
        await self.authorize(context)
        await self._household_manager().leave(context.actor_user_id, household_id)

    async def remove_household_member(
        self,
        context: RequestContext,
        household_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await self.authorize(context)
        await self._household_manager().remove_member(
            context.actor_user_id, household_id, user_id
        )

    def _ledger_manager(self) -> LedgerManagementService:
        return LedgerManagementService(
            self._session, currency=self._currency, timezone=self._timezone
        )

    def _household_manager(self) -> HouseholdManagementService:
        return HouseholdManagementService(
            self._session, currency=self._currency, timezone=self._timezone
        )
