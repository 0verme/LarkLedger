"""Financial goals on top of real ledger facts (P33-A).

``GoalService`` owns the goal lifecycle (create / list / get / update /
complete / archive / delete) and its account bindings. ``GoalProgressService``
derives progress **deterministically at read time** from the live balances of
the bound accounts and the trailing net-saving rate — the goal never holds a
parallel balance, never stores ``current_amount``, and never moves money.

Privacy contract (P32 carried forward):

* A goal is visible to an actor iff the actor can see **every** bound account.
  If a goal references any private account, non-owners simply get 404 — a goal
  display can never leak a private balance through a side channel.
* Personal ledgers keep exact legacy behavior (privacy is a no-op).
* Management (update / complete / archive / delete) is restricted to the goal
  creator or the ledger owner; every other ledger member reads only.
* All access is ledger-scoped; cross-ledger goal ids resolve to 404.

Multi-currency: every bound account must share the goal's currency and be a
``cash`` / ``asset`` account (liabilities are debt, not savings); different
currencies are never summed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Account,
    AccountType,
    Direction,
    FinancialGoal,
    GoalAccountBinding,
    GoalStatus,
    GoalType,
    LedgerEntry,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.member_resolution import MemberResolutionService
from lark_ledger.services.privacy import PrivacyService
from lark_ledger.services.transfers import TransferService
from lark_ledger.web_schemas import GoalAccountBindingItem, GoalProgress

MAX_MONEY = Decimal("999999999999.99")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 200
DEFAULT_FORECAST_DAYS = 90
MIN_FORECAST_HISTORY_DAYS = 30
DAYS_PER_MONTH = Decimal("30.4375")


class GoalError(ValueError):
    pass


class GoalNotFoundError(GoalError):
    """404 semantics: the goal does not exist, is in another ledger, or its
    bound accounts are not all visible to the actor."""


class GoalValidationError(GoalError):
    pass


class GoalConflictError(GoalError):
    pass


def _resolve_currency(value: str | None, default: str) -> str:
    code = (value or default).strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise GoalValidationError("币种必须是三位字母代码")
    return code


class GoalService:
    """Ledger-scoped, privacy-aware goal lifecycle commands."""

    def __init__(self, session: AsyncSession, *, timezone: str, currency: str) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency
        self._authorization = LedgerAuthorizationService(session)

    async def _authorize(self, context: RequestContext) -> None:
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)

    # -- lifecycle --------------------------------------------------------

    async def create(
        self,
        context: RequestContext,
        *,
        name: str,
        target_amount: Decimal,
        currency: str | None = None,
        description: str = "",
        target_date: date | None = None,
        account_ids: list[uuid.UUID] | None = None,
    ) -> FinancialGoal:
        await self._authorize(context)
        display = " ".join(name.strip().split())
        if not display or len(display) > MAX_NAME_LENGTH:
            raise GoalValidationError(f"目标名称不能为空且不超过 {MAX_NAME_LENGTH} 个字符")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise GoalValidationError(f"目标描述不能超过 {MAX_DESCRIPTION_LENGTH} 个字符")
        if target_amount <= 0 or target_amount > MAX_MONEY:
            raise GoalValidationError("目标金额必须大于 0 且在支持范围内")
        code = _resolve_currency(currency, self._currency)
        accounts = await self._validate_accounts(context, account_ids or [], code)

        goal = FinancialGoal(
            id=uuid.uuid4(),
            ledger_id=context.ledger_id,
            name=display,
            description=description.strip(),
            goal_type=GoalType.SAVINGS.value,
            target_amount=target_amount,
            currency=code,
            target_date=target_date,
            status=GoalStatus.ACTIVE.value,
            created_by_user_id=context.actor_user_id,
        )
        self._session.add(goal)
        await self._session.flush()
        for account in accounts:
            self._session.add(
                GoalAccountBinding(
                    id=uuid.uuid4(),
                    goal_id=goal.id,
                    ledger_id=context.ledger_id,
                    account_id=account.id,
                )
            )
        await self._session.flush()
        return goal

    async def list_goals(self, context: RequestContext) -> list[FinancialGoal]:
        """Goals visible to the actor in the current ledger (active first)."""
        await self._authorize(context)
        rows = list(
            (
                await self._session.scalars(
                    select(FinancialGoal)
                    .where(FinancialGoal.ledger_id == context.ledger_id)
                    .order_by(
                        FinancialGoal.status,
                        FinancialGoal.created_at,
                        FinancialGoal.id,
                    )
                )
            ).all()
        )
        if not rows:
            return []
        visible = await self._visible_goal_ids(context, rows)
        return [goal for goal in rows if goal.id in visible]

    async def get(self, context: RequestContext, goal_id: uuid.UUID) -> FinancialGoal:
        """404 when missing, cross-ledger, or any bound account is invisible."""
        await self._authorize(context)
        goal = await self._session.scalar(
            select(FinancialGoal).where(
                FinancialGoal.id == goal_id,
                FinancialGoal.ledger_id == context.ledger_id,
            )
        )
        if goal is None or not await self._goal_visible(context, goal):
            raise GoalNotFoundError("目标不存在或当前用户无权查看")
        return goal

    async def update(
        self,
        context: RequestContext,
        goal_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        target_amount: Decimal | None = None,
        currency: str | None = None,
        target_date: date | None = None,
        account_ids: list[uuid.UUID] | None = None,
        status: str | None = None,
    ) -> FinancialGoal:
        goal = await self._locked_managed(context, goal_id)
        if name is not None:
            display = " ".join(name.strip().split())
            if not display or len(display) > MAX_NAME_LENGTH:
                raise GoalValidationError(f"目标名称不能为空且不超过 {MAX_NAME_LENGTH} 个字符")
            goal.name = display
        if description is not None:
            if len(description) > MAX_DESCRIPTION_LENGTH:
                raise GoalValidationError(f"目标描述不能超过 {MAX_DESCRIPTION_LENGTH} 个字符")
            goal.description = description.strip()
        if target_amount is not None:
            if target_amount <= 0 or target_amount > MAX_MONEY:
                raise GoalValidationError("目标金额必须大于 0 且在支持范围内")
            goal.target_amount = target_amount
        if currency is not None:
            code = _resolve_currency(currency, goal.currency)
            goal.currency = code
        if target_date is not None:
            goal.target_date = target_date
        if status is not None:
            if status not in {
                GoalStatus.ACTIVE.value,
                GoalStatus.COMPLETED.value,
                GoalStatus.ARCHIVED.value,
            }:
                raise GoalValidationError("目标状态只能是 active / completed / archived")
            goal.status = status
        if account_ids is not None:
            accounts = await self._validate_accounts(context, account_ids, goal.currency)
            await self._session.execute(
                delete(GoalAccountBinding).where(
                    GoalAccountBinding.goal_id == goal_id,
                    GoalAccountBinding.ledger_id == context.ledger_id,
                )
            )
            for account in accounts:
                self._session.add(
                    GoalAccountBinding(
                        id=uuid.uuid4(),
                        goal_id=goal_id,
                        ledger_id=context.ledger_id,
                        account_id=account.id,
                    )
                )
        await self._session.flush()
        return goal

    async def complete(self, context: RequestContext, goal_id: uuid.UUID) -> FinancialGoal:
        goal = await self._locked_managed(context, goal_id)
        if goal.status == GoalStatus.ARCHIVED.value:
            raise GoalConflictError("已归档的目标不能标记完成")
        if goal.status == GoalStatus.COMPLETED.value:
            raise GoalConflictError("该目标已标记完成")
        goal.status = GoalStatus.COMPLETED.value
        await self._session.flush()
        return goal

    async def archive(self, context: RequestContext, goal_id: uuid.UUID) -> FinancialGoal:
        goal = await self._locked_managed(context, goal_id)
        if goal.status == GoalStatus.ARCHIVED.value:
            raise GoalConflictError("该目标已归档")
        goal.status = GoalStatus.ARCHIVED.value
        await self._session.flush()
        return goal

    async def delete(self, context: RequestContext, goal_id: uuid.UUID) -> None:
        """Hard delete the goal definition; bindings cascade, ledger data never
        touched (a goal is only a plan over real facts)."""
        goal = await self._locked_managed(context, goal_id)
        await self._session.delete(goal)
        await self._session.flush()

    # -- bindings & visibility --------------------------------------------

    async def _binding_rows(
        self, context: RequestContext, goal_id: uuid.UUID
    ) -> list[GoalAccountBinding]:
        return list(
            (
                await self._session.scalars(
                    select(GoalAccountBinding).where(
                        GoalAccountBinding.goal_id == goal_id,
                        GoalAccountBinding.ledger_id == context.ledger_id,
                    )
                )
            ).all()
        )

    async def binding_items(
        self, context: RequestContext, goal_id: uuid.UUID
    ) -> list[GoalAccountBindingItem]:
        rows = await self._binding_rows(context, goal_id)
        if not rows:
            return []
        account_ids = {row.account_id for row in rows}
        accounts = (
            await self._session.execute(
                select(Account.id, Account.name, Account.currency).where(
                    Account.ledger_id == context.ledger_id,
                    Account.id.in_(account_ids),
                )
            )
        ).all()
        by_id = {account_id: (name, currency) for account_id, name, currency in accounts}
        return [
            GoalAccountBindingItem(
                account_id=str(row.account_id),
                account_name=by_id[row.account_id][0] if row.account_id in by_id else None,
                currency=by_id[row.account_id][1] if row.account_id in by_id else "",
            )
            for row in rows
        ]

    async def _validate_accounts(
        self, context: RequestContext, account_ids: list[uuid.UUID], currency: str
    ) -> list[Account]:
        """Every bound account must be in this ledger, visible to the actor,
        same currency, and a savings-capable type (cash / asset)."""
        if not account_ids:
            raise GoalValidationError("储蓄目标至少绑定一个账户")
        if len(set(account_ids)) != len(account_ids):
            raise GoalValidationError("绑定账户不能重复")
        accounts: list[Account] = []
        service = AccountService(self._session)
        for account_id in account_ids:
            account = await service.get(context, account_id, require_active=True)
            if account.currency != currency:
                raise GoalValidationError(
                    f"目标币种 {currency} 与账户“{account.name}”币种 {account.currency} 不一致"
                )
            if account.type not in {AccountType.CASH.value, AccountType.ASSET.value}:
                raise GoalValidationError("储蓄目标只能绑定现金或资产账户，不能绑定负债账户")
            accounts.append(account)
        return accounts

    async def _goal_visible(self, context: RequestContext, goal: FinancialGoal) -> bool:
        """P32: a goal is visible iff the actor can see every bound account."""
        rows = await self._binding_rows(context, goal.id)
        if not rows:
            return True
        privacy = PrivacyService(self._session)
        if not await privacy.privacy_enabled(context):
            return True
        for row in rows:
            if not await privacy.can_view_account(context, row.account_id):
                return False
        return True

    async def _visible_goal_ids(
        self, context: RequestContext, goals: list[FinancialGoal]
    ) -> set[uuid.UUID]:
        """Batch visibility check for goal lists (one account query)."""
        goal_ids = [goal.id for goal in goals]
        if not goal_ids:
            return set()
        rows = list(
            (
                await self._session.scalars(
                    select(GoalAccountBinding).where(
                        GoalAccountBinding.ledger_id == context.ledger_id,
                        GoalAccountBinding.goal_id.in_(goal_ids),
                    )
                )
            ).all()
        )
        goals_with_bindings: set[uuid.UUID] = {row.goal_id for row in rows}
        # Goals without any binding behave like shared goals (visible to all
        # ledger members) — the service always creates at least one binding, but
        # legacy/hand-made rows stay safe.
        if not rows:
            return set(goal_ids)
        privacy = PrivacyService(self._session)
        if not await privacy.privacy_enabled(context):
            return set(goal_ids)
        visible_accounts = await privacy.visible_account_ids(context)
        hidden: set[uuid.UUID] = set()
        for row in rows:
            if row.account_id not in visible_accounts:
                hidden.add(row.goal_id)
        visible = {goal.id for goal in goals}
        visible -= hidden
        # Unbound goals stay visible.
        for goal_id in goal_ids:
            if goal_id not in goals_with_bindings:
                visible.add(goal_id)
        return visible

    async def _locked_managed(self, context: RequestContext, goal_id: uuid.UUID) -> FinancialGoal:
        """Authorize management: creator or ledger owner, goal visible, in-ledger."""
        await self._authorize(context)
        goal = await self._session.scalar(
            select(FinancialGoal)
            .where(
                FinancialGoal.id == goal_id,
                FinancialGoal.ledger_id == context.ledger_id,
            )
            .with_for_update()
        )
        if goal is None or not await self._goal_visible(context, goal):
            raise GoalNotFoundError("目标不存在或当前用户无权查看")
        roles = await MemberResolutionService(self._session).member_roles(context)
        is_owner = roles.get(context.actor_user_id) == "owner"
        if not (is_owner or goal.created_by_user_id == context.actor_user_id):
            raise GoalNotFoundError("目标不存在或当前用户无权查看")
        return goal


class GoalProgressService:
    """Deterministic progress derived from live ledger facts (P33-A)."""

    def __init__(self, session: AsyncSession, *, timezone: str, currency: str) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency
        self._authorization = LedgerAuthorizationService(session)

    async def progress(
        self,
        context: RequestContext,
        goal: FinancialGoal,
        *,
        now: datetime | None = None,
    ) -> GoalProgress:
        """Recompute one goal's progress from real account balances.

        No cached counter is involved: entry create / delete / restore and
        transfer create / reverse automatically change the result on the next
        read, which is exactly the P33 contract (a goal never has a writable
        ``current_amount``).
        """
        await self._authorization.get_accessible(context.actor_user_id, context.ledger_id)
        bindings = await GoalService(
            self._session, timezone=str(self._timezone), currency=self._currency
        )._binding_rows(context, goal.id)
        current = Decimal("0")
        transfers = TransferService(self._session)
        for binding in bindings:
            balance = await transfers.account_balance(context, binding.account_id)
            current += balance.current_balance
        target = goal.target_amount
        remaining = max(target - current, Decimal("0"))
        progress_ratio = (current / target).quantize(Decimal("0.0001")) if target else Decimal("0")
        progress_percent = (progress_ratio * 100).quantize(Decimal("0.01"))

        today = self._local(now or datetime.now(UTC)).date()
        days_remaining: int | None = None
        if goal.target_date is not None:
            days_remaining = (goal.target_date - today).days

        forecast = await self._forecast(
            context,
            goal,
            current_amount=current,
            target_amount=target,
            remaining=remaining,
            days_remaining=days_remaining,
            now=now,
        )

        return GoalProgress(
            goal_id=str(goal.id),
            name=goal.name,
            target_amount=target,
            current_amount=current,
            remaining_amount=remaining,
            progress_ratio=progress_ratio,
            progress_percent=progress_percent,
            currency=goal.currency,
            target_date=goal.target_date,
            days_remaining=days_remaining,
            is_target_reached=current >= target,
            **forecast,
        )

    async def _forecast(
        self,
        context: RequestContext,
        goal: FinancialGoal,
        *,
        current_amount: Decimal,
        target_amount: Decimal,
        remaining: Decimal,
        days_remaining: int | None,
        now: datetime | None,
    ) -> dict[str, Decimal | None]:
        """Very conservative deterministic forecast from the trailing
        net-saving rate. Returns ``None`` figures instead of guessing whenever
        history is insufficient, the rate is not positive, or the target is
        already reached."""
        rate = await self._monthly_saving_rate(context, now=now)
        if rate is None or rate <= 0 or remaining <= 0:
            return {
                "monthly_saving_rate": rate,
                "estimated_months_to_goal": None,
                "projected_shortfall_at_target_date": None,
            }
        estimated_months = (remaining / rate).quantize(Decimal("0.1"))
        shortfall: Decimal | None = None
        if goal.target_date is not None and days_remaining is not None and days_remaining > 0:
            months_to_date = Decimal(days_remaining) / Decimal("30.4375")
            projected_saved = rate * months_to_date
            if projected_saved < remaining:
                shortfall = (remaining - projected_saved).quantize(Decimal("0.01"))
        return {
            "monthly_saving_rate": rate,
            "estimated_months_to_goal": estimated_months,
            "projected_shortfall_at_target_date": shortfall,
        }

    async def _monthly_saving_rate(
        self, context: RequestContext, *, now: datetime | None = None
    ) -> Decimal | None:
        """Trailing net saving (income − expense) per month over the forecast
        window, privacy-filtered. ``None`` when the window has no data yet."""
        current = self._local(now or datetime.now(UTC))
        start_local = current - timedelta(days=DEFAULT_FORECAST_DAYS)
        start = start_local.astimezone(UTC)
        filters: list[Any] = [
            LedgerEntry.ledger_id == context.ledger_id,
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.occurred_at >= start,
            LedgerEntry.occurred_at < current.astimezone(UTC),
        ]
        from lark_ledger.services.privacy import PrivacyService

        privacy = await PrivacyService(self._session).entry_visibility_scope(context)
        if privacy is not None:
            filters.append(privacy)
        income, expense, first_at = (
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
                    func.min(LedgerEntry.occurred_at),
                ).where(*filters)
            )
        ).one()
        if first_at is None:
            return None
        # Require the history to actually reach back far enough; a brand-new
        # ledger with one week of data cannot drive a monthly rate.
        first_local = self._local(first_at).date()
        if (current.date() - first_local).days < MIN_FORECAST_HISTORY_DAYS:
            return None
        net = Decimal(income) - Decimal(expense)
        if net <= 0:
            return Decimal("0") if net == 0 else net
        elapsed_days = max((current.date() - first_local).days, 1)
        return (net / Decimal(elapsed_days) * DAYS_PER_MONTH).quantize(Decimal("0.01"))

    def _local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self._timezone)
