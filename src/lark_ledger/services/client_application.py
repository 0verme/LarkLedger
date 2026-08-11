from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountType,
    Direction,
    HouseholdInvitation,
    HouseholdMember,
    Ledger,
    PendingCommand,
    PendingStatus,
    RecurringRule,
    Transfer,
    TransferRevision,
)
from lark_ledger.schemas import Action, ExecutionResult, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.budget import BudgetService
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
from lark_ledger.services.recurring import RecurringService
from lark_ledger.services.transfers import AccountBalance, AssetSummary, TransferService
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
    HouseholdOverview,
    MemberStats,
    PendingDetail,
    PendingGroup,
    PendingPage,
    SortOrder,
    WebRecurringRule,
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
        return await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)

    async def list_ledgers(self, context: RequestContext) -> list[Ledger]:
        return await self._authorization.list_accessible(context.actor_user_id)

    async def list_personal_ledgers(self, context: RequestContext) -> list[Ledger]:
        await self.authorize(context)
        return await self._ledger_manager().list_owned(context.actor_user_id)

    async def current_ledger(self, context: RequestContext) -> Ledger:
        return await self.authorize(context)

    async def current_personal_ledger(self, context: RequestContext) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().get_owned(context.actor_user_id, context.ledger_id)

    async def find_personal_ledger(self, context: RequestContext, name: str) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().find_owned_by_name(context.actor_user_id, name)

    async def select_channel_ledger(self, context: RequestContext, ledger_id: uuid.UUID) -> Ledger:
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

    async def set_default_ledger(self, context: RequestContext, ledger_id: uuid.UUID) -> Ledger:
        await self.authorize(context)
        return await self._ledger_manager().set_default(context.actor_user_id, ledger_id)

    async def list_accounts(
        self, context: RequestContext, *, include_archived: bool = False
    ) -> list[Account]:
        return await AccountService(self._session).list(context, include_archived=include_archived)

    async def get_account(self, context: RequestContext, account_id: uuid.UUID) -> Account:
        return await AccountService(self._session).get(context, account_id)

    async def create_account(
        self,
        context: RequestContext,
        *,
        name: str,
        account_type: AccountType,
        subtype: str | None,
        provider: str | None,
        currency: str | None,
        opening_balance: Decimal,
        make_default: bool,
    ) -> Account:
        return await AccountService(self._session).create(
            context,
            name=name,
            account_type=account_type,
            subtype=subtype,
            provider=provider,
            currency=currency,
            opening_balance=opening_balance,
            make_default=make_default,
        )

    async def rename_account(
        self, context: RequestContext, account_id: uuid.UUID, name: str
    ) -> Account:
        return await AccountService(self._session).rename(context, account_id, name)

    async def archive_account(self, context: RequestContext, account_id: uuid.UUID) -> Account:
        return await AccountService(self._session).archive(context, account_id)

    async def set_default_account(self, context: RequestContext, account_id: uuid.UUID) -> Account:
        return await AccountService(self._session).set_default(context, account_id)

    async def create_transfer(
        self,
        context: RequestContext,
        *,
        from_account_id: uuid.UUID,
        to_account_id: uuid.UUID,
        amount: Decimal,
        occurred_at: datetime,
        note: str = "",
        source_type: str = "client",
        source_message_id: str | None = None,
        transfer_id: uuid.UUID | None = None,
    ) -> Transfer:
        return await TransferService(self._session).create(
            context,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount=amount,
            occurred_at=occurred_at,
            note=note,
            source_type=source_type,
            source_message_id=source_message_id,
            transfer_id=transfer_id,
        )

    async def get_transfer(self, context: RequestContext, transfer_id: uuid.UUID) -> Transfer:
        return await TransferService(self._session).get(context, transfer_id)

    async def list_transfers(
        self, context: RequestContext, *, page: int, page_size: int
    ) -> tuple[list[Transfer], int]:
        return await TransferService(self._session).list_paginated(
            context, page=page, page_size=page_size
        )

    async def transfer_revisions(
        self, context: RequestContext, transfer_id: uuid.UUID
    ) -> list[TransferRevision]:
        return await TransferService(self._session).revisions(context, transfer_id)

    async def reverse_transfer(self, context: RequestContext, transfer_id: uuid.UUID) -> Transfer:
        return await TransferService(self._session).reverse(context, transfer_id)

    async def account_balance(
        self, context: RequestContext, account_id: uuid.UUID
    ) -> AccountBalance:
        return await TransferService(self._session).account_balance(context, account_id)

    async def asset_summary(self, context: RequestContext) -> AssetSummary:
        return await TransferService(self._session).asset_summary(context)

    async def execute_financial(
        self,
        context: RequestContext,
        command: ParsedCommand,
        *,
        source_type: str,
        source_message_id: str | None = None,
        expected_updated_at: datetime | None = None,
        commit_changes: bool = True,
        account_id: uuid.UUID | None = None,
        paid_by_user_id: uuid.UUID | None = None,
        transfer_id: uuid.UUID | None = None,
        from_account_id: uuid.UUID | None = None,
        to_account_id: uuid.UUID | None = None,
    ) -> ExecutionResult:
        await self.authorize(context)
        if command.action is Action.TRANSFER:
            transfer_service = TransferService(self._session)
            if from_account_id is None:
                assert command.from_account_hint is not None
                from_account_id = (
                    await transfer_service.resolve_account_hint(context, command.from_account_hint)
                ).id
            if to_account_id is None:
                assert command.to_account_hint is not None
                to_account_id = (
                    await transfer_service.resolve_account_hint(context, command.to_account_hint)
                ).id
            assert command.amount is not None and command.occurred_at is not None
            row = await transfer_service.create(
                context,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount=command.amount,
                occurred_at=command.occurred_at,
                note=command.note or "",
                source_type=source_type,
                source_message_id=source_message_id,
                transfer_id=transfer_id,
            )
            if commit_changes:
                await self._session.commit()
            return ExecutionResult(message=f"转账已创建：{row.amount} {row.currency}")
        return await LedgerService(
            self._session,
            currency=self._currency,
            timezone=self._timezone,
            exchange_rates=self._exchange_rates,
            commit_changes=commit_changes,
            account_id=account_id,
            paid_by_user_id=paid_by_user_id,
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
            self._session, timezone=self._timezone, currency=self._currency
        ).dashboard(context)

    async def household_overview(
        self, context: RequestContext, *, period: date | None = None
    ) -> HouseholdOverview:
        """Deterministic overview for the current ledger (P31)."""
        await self.authorize(context)
        from lark_ledger.services.household_overview import HouseholdOverviewService

        return await HouseholdOverviewService(
            self._session, timezone=self._timezone, currency=self._currency
        ).overview(context, period=period)

    async def list_entries(self, context: RequestContext, query: EntryQuery) -> EntryPage:
        await self.authorize(context)
        return await WebLedgerQueryService(self._session, timezone=self._timezone).list_entries(
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

    async def entry_detail(self, context: RequestContext, short_id: str) -> EntryDetail | None:
        await self.authorize(context)
        return await WebLedgerQueryService(self._session, timezone=self._timezone).entry_detail(
            context, short_id
        )

    async def budgets(self, context: RequestContext) -> BudgetOverview:
        return await self.get_budget_overview(context)

    async def get_budget_overview(
        self, context: RequestContext, *, period: date | None = None
    ) -> BudgetOverview:
        await self.authorize(context)
        return await BudgetService(
            self._session, currency=self._currency, timezone=self._timezone
        ).overview(context, period=period)

    async def set_total_budget(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        amount: Decimal,
        currency: str | None = None,
    ) -> BudgetOverview:
        return await BudgetService(
            self._session, currency=self._currency, timezone=self._timezone
        ).set_total_budget(context, period=period, amount=amount, currency=currency)

    async def set_category_budget(
        self,
        context: RequestContext,
        *,
        period: date | None = None,
        category: str,
        amount: Decimal,
        currency: str | None = None,
    ) -> BudgetOverview:
        return await BudgetService(
            self._session, currency=self._currency, timezone=self._timezone
        ).set_category_budget(
            context, period=period, category=category, amount=amount, currency=currency
        )

    async def delete_budget(
        self, context: RequestContext, *, period: date | None = None, category: str | None = None
    ) -> BudgetOverview:
        return await BudgetService(
            self._session, currency=self._currency, timezone=self._timezone
        ).delete_budget(context, period=period, category=category)

    # -- recurring rules (P29) --------------------------------------------

    def _recurring(self) -> RecurringService:
        return RecurringService(
            self._session, currency=self._currency, timezone=self._timezone
        )

    async def create_recurring_rule(
        self,
        context: RequestContext,
        *,
        transaction_type: Direction,
        amount: Decimal,
        currency: str | None,
        category: str,
        description: str,
        frequency: str,
        interval: int,
        next_occurrence: date,
        account_id: uuid.UUID,
        paid_by_user_id: uuid.UUID | None = None,
    ) -> RecurringRule:
        from lark_ledger.models import RecurringFrequency

        return await self._recurring().create(
            context,
            transaction_type=transaction_type,
            amount=amount,
            currency=currency,
            category=category,
            description=description,
            frequency=RecurringFrequency(frequency),
            interval=interval,
            next_occurrence=next_occurrence,
            account_id=account_id,
            paid_by_user_id=paid_by_user_id,
        )

    async def get_recurring_rule(
        self, context: RequestContext, rule_id: uuid.UUID
    ) -> RecurringRule:
        return await self._recurring().get(context, rule_id)

    async def list_recurring_rules(self, context: RequestContext) -> list[RecurringRule]:
        return await self._recurring().list(context)

    async def update_recurring_rule(
        self,
        context: RequestContext,
        rule_id: uuid.UUID,
        *,
        transaction_type: Direction | None,
        amount: Decimal | None,
        currency: str | None,
        category: str | None,
        description: str | None,
        frequency: str | None,
        interval: int | None,
        next_occurrence: date | None,
        account_id: uuid.UUID | None,
        paid_by_user_id: uuid.UUID | None = None,
    ) -> RecurringRule:
        from lark_ledger.models import RecurringFrequency

        service = self._recurring()
        return await service.update(
            context,
            rule_id,
            transaction_type=transaction_type,
            amount=amount,
            currency=currency,
            category=category,
            description=description,
            frequency=RecurringFrequency(frequency) if frequency is not None else None,
            interval=interval,
            next_occurrence=next_occurrence,
            account_id=account_id,
            paid_by_user_id=paid_by_user_id,
        )

    async def pause_recurring_rule(
        self, context: RequestContext, rule_id: uuid.UUID
    ) -> RecurringRule:
        return await self._recurring().pause(context, rule_id)

    async def resume_recurring_rule(
        self, context: RequestContext, rule_id: uuid.UUID
    ) -> RecurringRule:
        return await self._recurring().resume(context, rule_id)

    async def disable_recurring_rule(
        self, context: RequestContext, rule_id: uuid.UUID
    ) -> RecurringRule:
        return await self._recurring().disable(context, rule_id)

    async def skip_recurring_occurrence(
        self, context: RequestContext, rule_id: uuid.UUID
    ) -> RecurringRule:
        return await self._recurring().skip_occurrence(context, rule_id)

    async def recurring_rule_views(self, context: RequestContext) -> list[WebRecurringRule]:
        """Return the enriched recurring-rule list for the Web dashboard.

        Account names and waiting-confirmation counts are denormalized at read
        time and never leak other ledgers' accounts.
        """
        rules = await self.list_recurring_rules(context)
        account_names = await self._recurring_account_names(context, rules)
        pending_counts = await self._recurring_pending_counts(context, rules)
        return [
            self._web_recurring_rule(
                rule,
                account_names.get(rule.account_id),
                pending_counts.get(rule.id, 0),
            )
            for rule in rules
        ]

    async def recurring_rule_view(
        self, context: RequestContext, rule_id: uuid.UUID
    ) -> WebRecurringRule:
        rule = await self.get_recurring_rule(context, rule_id)
        names = await self._recurring_account_names(context, [rule])
        counts = await self._recurring_pending_counts(context, [rule])
        return self._web_recurring_rule(
            rule, names.get(rule.account_id), counts.get(rule.id, 0)
        )

    @staticmethod
    def _web_recurring_rule(
        rule: RecurringRule, account_name: str | None, pending_count: int
    ) -> WebRecurringRule:
        return WebRecurringRule(
            id=str(rule.id),
            ledger_id=str(rule.ledger_id),
            transaction_type=rule.transaction_type.value,
            amount=rule.amount,
            currency=rule.currency,
            category=rule.category,
            description=rule.description,
            frequency=rule.frequency,
            interval=rule.interval,
            next_occurrence=rule.next_occurrence,
            status=rule.status,
            account_id=str(rule.account_id),
            account_name=account_name,
            paid_by_user_id=(
                str(rule.paid_by_user_id) if rule.paid_by_user_id is not None else None
            ),
            pending_count=pending_count,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    async def _recurring_account_names(
        self, context: RequestContext, rules: list[RecurringRule]
    ) -> dict[uuid.UUID, str]:
        account_ids = {rule.account_id for rule in rules if rule.account_id is not None}
        names: dict[uuid.UUID, str] = {}
        if not account_ids:
            return names
        rows = (
            await self._session.execute(
                select(Account.id, Account.name).where(
                    Account.ledger_id == context.ledger_id,
                    Account.id.in_(account_ids),
                )
            )
        ).all()
        for account_id, name in rows:
            names[account_id] = name
        return names

    async def _recurring_pending_counts(
        self, context: RequestContext, rules: list[RecurringRule]
    ) -> dict[uuid.UUID, int]:
        if not rules:
            return {}
        rule_ids = [rule.id for rule in rules]
        rows = (
            await self._session.execute(
                select(
                    PendingCommand.recurring_rule_id,
                    func.count(PendingCommand.id),
                )
                .where(
                    PendingCommand.recurring_rule_id.in_(rule_ids),
                    PendingCommand.ledger_id == context.ledger_id,
                    PendingCommand.status.in_(
                        [
                            PendingStatus.PENDING.value,
                            PendingStatus.EXECUTING.value,
                        ]
                    ),
                )
                .group_by(PendingCommand.recurring_rule_id)
            )
        ).all()
        return {rule_id: int(count) for rule_id, count in rows if rule_id is not None}

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
        return await self._household_manager().rename(context.actor_user_id, household_id, name)

    async def list_household_members(
        self, context: RequestContext, household_id: uuid.UUID
    ) -> list[HouseholdMemberView]:
        await self.authorize(context)
        return await self._household_manager().list_members(context.actor_user_id, household_id)

    async def invite_household_member(
        self, context: RequestContext, household_id: uuid.UUID, target: str
    ) -> HouseholdInvitation:
        await self.authorize(context)
        return await self._household_manager().invite(context.actor_user_id, household_id, target)

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

    async def leave_household(self, context: RequestContext, household_id: uuid.UUID) -> None:
        await self.authorize(context)
        await self._household_manager().leave(context.actor_user_id, household_id)

    async def remove_household_member(
        self,
        context: RequestContext,
        household_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await self.authorize(context)
        await self._household_manager().remove_member(context.actor_user_id, household_id, user_id)

    async def set_household_member_alias(
        self,
        context: RequestContext,
        household_id: uuid.UUID,
        user_id: uuid.UUID,
        alias: str,
    ) -> HouseholdMember:
        await self.authorize(context)
        return await self._household_manager().set_member_alias(
            context.actor_user_id, household_id, user_id, alias
        )

    async def member_stats(
        self, context: RequestContext, ledger_id: uuid.UUID
    ) -> list[MemberStats]:
        """Member contribution stats for ``ledger_id`` (P30), privacy-filtered.

        The named ledger must be accessible to the actor; the aggregation runs
        against that ledger even when it is not the actor's current one.
        """
        await self._authorization.get_accessible(context.actor_user_id, ledger_id)
        from lark_ledger.services.member_stats import MemberStatsService

        target = RequestContext(
            actor_user_id=context.actor_user_id,
            ledger_id=ledger_id,
            source_channel=context.source_channel,
            channel_identity_id=context.channel_identity_id,
            external_subject_id=context.external_subject_id,
        )
        return await MemberStatsService(self._session).stats(target)

    def _ledger_manager(self) -> LedgerManagementService:
        return LedgerManagementService(
            self._session, currency=self._currency, timezone=self._timezone
        )

    def _household_manager(self) -> HouseholdManagementService:
        return HouseholdManagementService(
            self._session, currency=self._currency, timezone=self._timezone
        )
