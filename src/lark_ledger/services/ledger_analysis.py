"""Deterministic comparison and cash-flow facts for assistant queries (#16)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Account, Direction, LedgerEntry, Transfer
from lark_ledger.query_schemas import (
    MAX_QUERY_PROVENANCE_IDS,
    QueryAggregation,
    QueryAnalysisResult,
    QueryComparisonGroup,
    QueryDistribution,
    QueryFactTotals,
    QueryFilters,
    QueryGrouping,
    QueryMetric,
    QueryMode,
    QueryPeriod,
    QueryPlan,
    QueryProvenance,
    QueryResult,
    QueryStatus,
    QueryTrendPoint,
)
from lark_ledger.services.privacy import PrivacyService

EntryFact = tuple[str, Decimal, Direction, str, datetime, Any]
TransferFact = tuple[Decimal, Any, Any, datetime]


class LedgerAnalysisService:
    """Produce comparison facts without letting the model do arithmetic."""

    def __init__(
        self, session: AsyncSession, *, timezone: str, currency: str
    ) -> None:
        self._session = session
        self._timezone = ZoneInfo(timezone)
        self._currency = currency

    async def execute(self, context: RequestContext, plan: QueryPlan) -> QueryResult:
        analysis = plan.analysis
        if analysis is None:
            raise ValueError("analysis plan is required")
        if plan.start is None or plan.end is None:
            return self._invalid(plan, "analysis requires an explicit current range")
        if plan.mode is QueryMode.GROUP and analysis.metric in {
            QueryMetric.CASH_OUTFLOW,
            QueryMetric.CASH_INFLOW,
        }:
            return QueryResult(
                status=QueryStatus.UNSUPPORTED,
                mode=plan.mode,
                message="现金流语义暂不支持按分组比较。",
                unsupported=[f"metric:{analysis.metric.value}"],
            )

        current_entries = await self._entry_facts(context, plan, plan.start, plan.end)
        current_transfers = await self._transfer_facts(context, plan, plan.start, plan.end)
        current = self._totals(current_entries, current_transfers, plan)

        baseline: QueryFactTotals | None = None
        baseline_entries: list[EntryFact] = []
        baseline_transfers: list[TransferFact] = []
        baseline_available = True
        unavailable_reason: str | None = None
        if analysis.baseline_start is not None and analysis.baseline_end is not None:
            baseline_entries = await self._entry_facts(
                context, plan, analysis.baseline_start, analysis.baseline_end
            )
            baseline_transfers = await self._transfer_facts(
                context, plan, analysis.baseline_start, analysis.baseline_end
            )
            if baseline_entries or baseline_transfers:
                baseline = self._totals(
                    baseline_entries, baseline_transfers, plan
                )
            else:
                baseline_available = False
                unavailable_reason = "基准时间范围没有足够的账目或转账数据。"

        current_value = self._metric_value(current, analysis.metric)
        assert current_value is not None
        baseline_value = self._metric_value(baseline, analysis.metric) if baseline else None
        delta = current_value - baseline_value if baseline_value is not None else None
        percentage = None
        if delta is not None and baseline_value is not None and baseline_value != Decimal("0"):
            percentage = delta / baseline_value * Decimal("100")

        distribution_entries = current_entries
        distribution_baseline_entries = baseline_entries
        grouping = plan.grouping or QueryGrouping.CATEGORY
        if plan.mode is QueryMode.GROUP:
            distribution_grouping = grouping
        else:
            distribution_grouping = QueryGrouping.CATEGORY
        names = await self._account_names(
            {fact[5] for fact in distribution_entries if fact[5] is not None}
            | {fact[5] for fact in distribution_baseline_entries if fact[5] is not None}
        )
        current_groups = self._group_facts(
            distribution_entries, analysis.metric, distribution_grouping, names
        )
        baseline_groups = self._group_facts(
            distribution_baseline_entries, analysis.metric, distribution_grouping, names
        )
        top_contributors = self._comparison_groups(
            current_groups, baseline_groups, limit=plan.top_n or 10
        )
        distribution = self._distribution(current_groups, current_value)
        trend = self._trend(current_entries, current_transfers, plan)
        facts = QueryAnalysisResult(
            metric=analysis.metric,
            current=current,
            baseline=baseline,
            delta=delta,
            percentage=percentage,
            baseline_available=baseline_available,
            unavailable_reason=unavailable_reason,
            top_contributors=top_contributors,
            distribution=distribution,
            trend=trend,
        )

        source_ids = [fact[0] for fact in current_entries[:MAX_QUERY_PROVENANCE_IDS]]
        status = QueryStatus.OK if current_entries or current_transfers else QueryStatus.EMPTY
        return QueryResult(
            status=status,
            mode=plan.mode,
            message=self._message(facts),
            total_count=len(current_entries),
            period=self._period(plan),
            filters=self._filters(plan),
            provenance=QueryProvenance(
                period=self._period(plan),
                filters=self._filters(plan),
                aggregation=QueryAggregation(
                    mode=plan.mode,
                    grouping=plan.grouping,
                    metric=analysis.metric.value,
                ),
                source_count=len(current_entries),
                source_short_ids=source_ids,
                source_ids_truncated=len(current_entries) > len(source_ids),
                as_of=datetime.now(UTC),
            ),
            analysis=facts,
        )

    async def _entry_facts(
        self,
        context: RequestContext,
        plan: QueryPlan,
        start: datetime,
        end: datetime,
    ) -> list[EntryFact]:
        filters = await self._entry_filters(context, plan, start, end)
        rows = (
            await self._session.execute(
                select(
                    LedgerEntry.short_id,
                    LedgerEntry.amount,
                    LedgerEntry.direction,
                    LedgerEntry.category,
                    LedgerEntry.occurred_at,
                    LedgerEntry.account_id,
                )
                .where(*filters)
                .order_by(
                    LedgerEntry.occurred_at.desc(),
                    LedgerEntry.created_at.desc(),
                    LedgerEntry.id.desc(),
                )
            )
        ).all()
        return [
            (short_id, Decimal(amount), direction, str(category), occurred_at, account_id)
            for short_id, amount, direction, category, occurred_at, account_id in rows
        ]

    async def _transfer_facts(
        self,
        context: RequestContext,
        plan: QueryPlan,
        start: datetime,
        end: datetime,
    ) -> list[TransferFact]:
        filters: list[Any] = [
            Transfer.ledger_id == plan.ledger_id,
            Transfer.reversed_at.is_(None),
            Transfer.occurred_at >= start,
            Transfer.occurred_at < end,
        ]
        if plan.account_id is not None:
            filters.append(
                or_(
                    Transfer.from_account_id == plan.account_id,
                    Transfer.to_account_id == plan.account_id,
                )
            )
        privacy = PrivacyService(self._session)
        if await privacy.privacy_enabled(context):
            filters.append(
                and_(
                    privacy.account_visible_exists(context, Transfer.from_account_id),
                    privacy.account_visible_exists(context, Transfer.to_account_id),
                )
            )
        rows = (
            await self._session.execute(
                select(
                    Transfer.amount,
                    Transfer.from_account_id,
                    Transfer.to_account_id,
                    Transfer.occurred_at,
                ).where(*filters)
            )
        ).all()
        return [
            (Decimal(amount), from_account_id, to_account_id, occurred_at)
            for amount, from_account_id, to_account_id, occurred_at in rows
        ]

    async def _entry_filters(
        self,
        context: RequestContext,
        plan: QueryPlan,
        start: datetime,
        end: datetime,
    ) -> list[Any]:
        filters: list[Any] = [
            self._entry_scope(context),
            LedgerEntry.deleted_at.is_(None),
            LedgerEntry.occurred_at >= start,
            LedgerEntry.occurred_at < end,
        ]
        privacy = await PrivacyService(self._session).entry_visibility_scope(context)
        if privacy is not None:
            filters.append(privacy)
        if plan.direction is not None:
            filters.append(LedgerEntry.direction == plan.direction)
        if plan.category is not None:
            filters.append(LedgerEntry.category == plan.category)
        if plan.account_id is not None:
            filters.append(LedgerEntry.account_id == plan.account_id)
        if plan.min_amount is not None:
            filters.append(LedgerEntry.amount >= plan.min_amount)
        if plan.max_amount is not None:
            filters.append(LedgerEntry.amount <= plan.max_amount)
        if plan.keyword:
            filters.append(
                or_(
                    LedgerEntry.note.icontains(plan.keyword, autoescape=True),
                    LedgerEntry.category.icontains(plan.keyword, autoescape=True),
                )
            )
        return filters

    @staticmethod
    def _entry_scope(context: RequestContext) -> Any:
        if context.external_subject_id is None:
            return LedgerEntry.ledger_id == context.ledger_id
        return or_(
            LedgerEntry.ledger_id == context.ledger_id,
            and_(
                LedgerEntry.ledger_id.is_(None),
                LedgerEntry.user_open_id == context.external_subject_id,
            ),
        )

    def _totals(
        self,
        entries: list[EntryFact],
        transfers: list[TransferFact],
        plan: QueryPlan,
    ) -> QueryFactTotals:
        consumption = sum(
            (amount for _, amount, direction, *_ in entries if direction is Direction.EXPENSE),
            Decimal("0"),
        )
        income = sum(
            (amount for _, amount, direction, *_ in entries if direction is Direction.INCOME),
            Decimal("0"),
        )
        transfer_out = Decimal("0")
        transfer_in = Decimal("0")
        for amount, from_account_id, to_account_id, _occurred_at in transfers:
            if plan.account_id is None or from_account_id == plan.account_id:
                transfer_out += amount
            if plan.account_id is None or to_account_id == plan.account_id:
                transfer_in += amount
        return QueryFactTotals(
            currency=self._currency,
            consumption=consumption,
            income=income,
            transfer_out=transfer_out,
            transfer_in=transfer_in,
            cash_outflow=consumption + transfer_out,
            cash_inflow=income + transfer_in,
            entry_count=len(entries),
            transfer_count=len(transfers),
        )

    @staticmethod
    def _metric_value(
        facts: QueryFactTotals | None, metric: QueryMetric
    ) -> Decimal | None:
        if facts is None:
            return None
        return {
            QueryMetric.EXPENSE: facts.consumption,
            QueryMetric.INCOME: facts.income,
            QueryMetric.CASH_OUTFLOW: facts.cash_outflow,
            QueryMetric.CASH_INFLOW: facts.cash_inflow,
        }[metric]

    async def _account_names(self, account_ids: set[Any]) -> dict[Any, str]:
        if not account_ids:
            return {}
        rows = (
            await self._session.execute(
                select(Account.id, Account.name).where(Account.id.in_(account_ids))
            )
        ).all()
        return {account_id: name for account_id, name in rows}

    def _group_facts(
        self,
        entries: list[EntryFact],
        metric: QueryMetric,
        grouping: QueryGrouping,
        names: dict[Any, str],
    ) -> dict[str, tuple[Decimal, int]]:
        totals: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        counts: defaultdict[str, int] = defaultdict(int)
        for _short_id, amount, direction, category, occurred_at, account_id in entries:
            if metric is QueryMetric.EXPENSE and direction is not Direction.EXPENSE:
                continue
            if metric is QueryMetric.INCOME and direction is not Direction.INCOME:
                continue
            if metric in {QueryMetric.CASH_OUTFLOW, QueryMetric.CASH_INFLOW}:
                continue
            key = self._group_key(grouping, category, account_id, occurred_at, names)
            totals[key] += amount
            counts[key] += 1
        return {key: (amount, counts[key]) for key, amount in totals.items()}

    def _comparison_groups(
        self,
        current: dict[str, tuple[Decimal, int]],
        baseline: dict[str, tuple[Decimal, int]],
        *,
        limit: int,
    ) -> list[QueryComparisonGroup]:
        keys = set(current) | set(baseline)
        rows = []
        for key in keys:
            current_amount, current_count = current.get(key, (Decimal("0"), 0))
            baseline_amount, baseline_count = baseline.get(key, (Decimal("0"), 0))
            rows.append(
                QueryComparisonGroup(
                    key=key,
                    current=current_amount,
                    baseline=baseline_amount,
                    delta=current_amount - baseline_amount,
                    current_count=current_count,
                    baseline_count=baseline_count,
                )
            )
        return sorted(rows, key=lambda row: (-row.delta, row.key))[:limit]

    @staticmethod
    def _distribution(
        groups: dict[str, tuple[Decimal, int]], total: Decimal
    ) -> list[QueryDistribution]:
        rows = [
            QueryDistribution(
                key=key,
                amount=amount,
                ratio=(amount / total * Decimal("100") if total else Decimal("0")),
                count=count,
            )
            for key, (amount, count) in groups.items()
        ]
        return sorted(rows, key=lambda row: (-row.amount, row.key))[:50]

    def _trend(
        self,
        entries: list[EntryFact],
        transfers: list[TransferFact],
        plan: QueryPlan,
    ) -> list[QueryTrendPoint]:
        assert plan.start is not None and plan.end is not None
        local_start = self._local(plan.start)
        local_end = self._local(plan.end)
        span = local_end - local_start
        grouping = (
            "day"
            if span <= timedelta(days=31)
            else "week"
            if span <= timedelta(days=92)
            else "month"
        )
        values: defaultdict[str, list[Decimal]] = defaultdict(
            lambda: [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")]
        )
        for _short_id, amount, direction, _category, occurred_at, _account_id in entries:
            key = self._trend_key(occurred_at, grouping)
            if direction is Direction.EXPENSE:
                values[key][0] += amount
            else:
                values[key][1] += amount
        for amount, from_account_id, to_account_id, occurred_at in transfers:
            key = self._trend_key(occurred_at, grouping)
            if plan.account_id is None or from_account_id == plan.account_id:
                values[key][2] += amount
            if plan.account_id is None or to_account_id == plan.account_id:
                values[key][3] += amount
        return [
            QueryTrendPoint(
                period=key,
                consumption=parts[0],
                income=parts[1],
                transfer_out=parts[2],
                transfer_in=parts[3],
            )
            for key, parts in sorted(values.items())
        ]

    def _group_key(
        self,
        grouping: QueryGrouping,
        category: str,
        account_id: Any,
        occurred_at: datetime,
        names: dict[Any, str],
    ) -> str:
        if grouping is QueryGrouping.CATEGORY:
            return category
        if grouping is QueryGrouping.ACCOUNT:
            return names.get(account_id, "")
        local_date = self._local(occurred_at).date()
        if grouping is QueryGrouping.DAY:
            return local_date.isoformat()
        if grouping is QueryGrouping.WEEK:
            return (local_date - timedelta(days=local_date.weekday())).isoformat()
        return local_date.strftime("%Y-%m")

    def _trend_key(self, occurred_at: datetime, grouping: str) -> str:
        local_date = self._local(occurred_at).date()
        if grouping == "day":
            return local_date.isoformat()
        if grouping == "week":
            return (local_date - timedelta(days=local_date.weekday())).isoformat()
        return local_date.strftime("%Y-%m")

    def _local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(self._timezone)

    @staticmethod
    def _period(plan: QueryPlan) -> QueryPeriod:
        return QueryPeriod(start=plan.start, end=plan.end, timezone=plan.timezone)

    @staticmethod
    def _filters(plan: QueryPlan) -> QueryFilters:
        return QueryFilters(
            direction=plan.direction,
            category=plan.category,
            account_id=plan.account_id,
            account_name=plan.account_name,
            min_amount=plan.min_amount,
            max_amount=plan.max_amount,
            keyword=plan.keyword,
            sort=plan.sort,
            order=plan.order,
            grouping=plan.grouping,
            top_n=plan.top_n,
            privacy_applied=plan.privacy_applied,
        )

    def _message(self, facts: QueryAnalysisResult) -> str:
        current = self._metric_value(facts.current, facts.metric) or Decimal("0")
        label = {
            QueryMetric.EXPENSE: "消费",
            QueryMetric.INCOME: "收入",
            QueryMetric.CASH_OUTFLOW: "现金流出",
            QueryMetric.CASH_INFLOW: "现金流入",
        }[facts.metric]
        message = f"本期{label} {self._money(current)}"
        if facts.baseline is not None and facts.delta is not None:
            direction = "增加" if facts.delta >= 0 else "减少"
            message += f"，较基准期{direction} {self._money(abs(facts.delta))}"
            if facts.percentage is not None:
                message += f"（{abs(facts.percentage):.2f}%）"
        elif not facts.baseline_available:
            message += f"；{facts.unavailable_reason}"
        message += (
            f"。消费 {self._money(facts.current.consumption)}，收入 "
            f"{self._money(facts.current.income)}，账户转移流出 "
            f"{self._money(facts.current.transfer_out)}。"
        )
        return message

    def _money(self, value: Decimal) -> str:
        if self._currency == "CNY":
            return f"¥{value:.2f}"
        return f"{value:.2f} {self._currency}"

    @staticmethod
    def _invalid(plan: QueryPlan, reason: str) -> QueryResult:
        return QueryResult(
            status=QueryStatus.INVALID,
            mode=plan.mode,
            message="查询条件无效。",
            invalid_reason=reason,
        )
