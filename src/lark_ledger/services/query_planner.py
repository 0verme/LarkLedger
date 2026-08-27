"""P46 — SQL-free query planning: turn a ``QueryIntent`` into a ``QueryPlan``.

The planner is the only place that resolves names, normalizes the time range
and records the privacy marker. It never executes a query and never exposes
SQL; ``LedgerQueryService`` consumes the deterministic ``QueryPlan``.

Every outcome is controlled:

* authorization failures raise ``LedgerQueryAccessDeniedError`` (the adapter /
  the existing AI pipeline maps it, exactly like ``LedgerAccessDeniedError``);
* unknown / ambiguous / invisible accounts return a structured
  ``QueryResult(status=needs_clarification)`` — never a guessed account;
* planning-rule violations (range > MAX_QUERY_RANGE_DAYS, bad timezone) return
  ``QueryResult(status=invalid)`` with ``invalid_reason``.

Privacy: the planner records ``privacy_applied``; the **actual** SQL visibility
condition is applied inside ``LedgerQueryService`` before any aggregation, so a
private account can never leak through totals / counts / groups / Top N.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Account
from lark_ledger.query_schemas import (
    MAX_QUERY_RANGE_DAYS,
    QueryAnalysis,
    QueryFilters,
    QueryIntent,
    QueryMode,
    QueryPeriod,
    QueryPlan,
    QueryResult,
    QueryStatus,
)
from lark_ledger.services.accounts import (
    MAX_ACCOUNT_NAME_LENGTH,
    AccountError,
    normalize_account_name,
)
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.privacy import PrivacyService


class LedgerQueryAccessDeniedError(PermissionError):
    """The actor is not allowed to query the requested ledger (authorization,
    not a query-semantics status — adapters map it the way they map
    ``LedgerAccessDeniedError``)."""


class QueryPlanner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        timezone: str = "Asia/Shanghai",
        currency: str = "CNY",
    ) -> None:
        self._session = session
        self._timezone = timezone
        self._currency = currency

    async def plan(
        self, context: RequestContext, intent: QueryIntent
    ) -> QueryPlan | QueryResult:
        try:
            await LedgerAuthorizationService(self._session).get_accessible(
                context.actor_user_id, context.ledger_id
            )
        except Exception as exc:
            raise LedgerQueryAccessDeniedError(
                "actor cannot access the requested ledger"
            ) from exc

        try:
            zone = ZoneInfo(intent.timezone or self._timezone)
        except Exception:
            return self._invalid(intent, "unsupported timezone")

        normalized = self._normalize_range(intent, zone)
        if isinstance(normalized, QueryResult):
            return normalized
        start, end = normalized

        analysis = self._normalize_analysis(intent, zone)
        if isinstance(analysis, QueryResult):
            return analysis

        resolved = await self._resolve_account(context, intent.account)
        if isinstance(resolved, QueryResult):
            return resolved
        account_id, account_name = resolved

        privacy_applied = (
            await PrivacyService(self._session).entry_visibility_scope(context)
        ) is not None

        return QueryPlan(
            mode=intent.mode,
            ledger_id=context.ledger_id,
            start=start,
            end=end,
            timezone=zone.key,
            direction=intent.direction,
            category=intent.category,
            account_id=account_id,
            account_name=account_name,
            min_amount=intent.min_amount,
            max_amount=intent.max_amount,
            keyword=intent.keyword,
            sort=intent.sort,
            order=intent.order,
            grouping=intent.grouping,
            top_n=intent.top_n,
            page=intent.page,
            page_size=intent.page_size,
            privacy_applied=privacy_applied,
            analysis=analysis,
        )

    # ------------------------------------------------------------------ #
    # Range / timezone
    # ------------------------------------------------------------------ #

    def _normalize_range(
        self, intent: QueryIntent, zone: ZoneInfo
    ) -> tuple[datetime | None, datetime | None] | QueryResult:
        if intent.start is None or intent.end is None:
            return None, None
        start = intent.start.astimezone(UTC)
        end = intent.end.astimezone(UTC)
        if start >= end:
            return self._invalid(intent, "time range must be increasing: [start, end)")
        if (end.astimezone(zone) - start.astimezone(zone)) > timedelta(
            days=MAX_QUERY_RANGE_DAYS
        ):
            return self._invalid(
                intent, f"time range must not exceed {MAX_QUERY_RANGE_DAYS} days"
            )
        return start, end

    def _normalize_analysis(
        self, intent: QueryIntent, zone: ZoneInfo
    ) -> QueryAnalysis | None | QueryResult:
        """Normalize an optional comparison baseline to UTC.

        The current query range and the baseline use the same explicit
        left-closed/right-open and civil-day ceiling semantics.  Keeping this
        normalization in the planner prevents the analysis service from
        interpreting model-provided timestamps a second time.
        """
        if intent.analysis is None:
            return None
        baseline_start = intent.analysis.baseline_start
        baseline_end = intent.analysis.baseline_end
        if baseline_start is None or baseline_end is None:
            return intent.analysis
        start = baseline_start.astimezone(UTC)
        end = baseline_end.astimezone(UTC)
        if (end.astimezone(zone) - start.astimezone(zone)) > timedelta(
            days=MAX_QUERY_RANGE_DAYS
        ):
            return self._invalid(
                intent, f"baseline range must not exceed {MAX_QUERY_RANGE_DAYS} days"
            )
        return intent.analysis.model_copy(
            update={"baseline_start": start, "baseline_end": end}
        )

    # ------------------------------------------------------------------ #
    # Account name resolution (ledger-scoped + privacy-visible + unique)
    # ------------------------------------------------------------------ #

    async def _resolve_account(
        self, context: RequestContext, hint: str | None
    ) -> tuple[uuid.UUID | None, str | None] | QueryResult:
        if hint is None:
            return None, None
        if len(hint) > MAX_ACCOUNT_NAME_LENGTH:
            return self._clarification(["account name is too long"])
        try:
            _, normalized = normalize_account_name(hint)
        except AccountError:
            return self._clarification(["account name is invalid"])
        query = select(Account).where(
            Account.ledger_id == context.ledger_id,
            Account.normalized_name == normalized,
        )
        privacy = PrivacyService(self._session)
        if await privacy.privacy_enabled(context):
            # A private account owned by another household member must not
            # resolve — and the controlled message must not reveal its
            # existence either.
            query = query.where(privacy.account_visibility_scope(context))
        rows = list((await self._session.scalars(query)).all())
        if len(rows) != 1:
            return self._clarification(["account name is unknown or ambiguous"])
        account = rows[0]
        return account.id, account.name

    # ------------------------------------------------------------------ #
    # Controlled outcomes
    # ------------------------------------------------------------------ #

    @classmethod
    def _clarification(cls, reasons: list[str]) -> QueryResult:
        return QueryResult(
            status=QueryStatus.NEEDS_CLARIFICATION,
            mode=QueryMode.LIST,
            message="请明确查询条件后重试。",
            clarification=reasons,
        )

    @classmethod
    def _invalid(cls, intent: QueryIntent, reason: str) -> QueryResult:
        return QueryResult(
            status=QueryStatus.INVALID,
            mode=intent.mode,
            message="查询条件无效。",
            invalid_reason=reason,
            period=QueryPeriod(
                start=intent.start.astimezone(UTC) if intent.start is not None else None,
                end=intent.end.astimezone(UTC) if intent.end is not None else None,
                timezone=intent.timezone or "",
            ),
            filters=cls._filters(intent),
        )

    @staticmethod
    def _filters(intent: QueryIntent) -> QueryFilters:
        return QueryFilters(
            direction=intent.direction,
            category=intent.category,
            account_id=None,
            account_name=None,
            min_amount=intent.min_amount,
            max_amount=intent.max_amount,
            keyword=intent.keyword,
            sort=intent.sort,
            order=intent.order,
            grouping=intent.grouping,
            top_n=intent.top_n,
            privacy_applied=False,
        )
