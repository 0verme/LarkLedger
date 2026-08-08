"""Direct service-level coverage for ``WebLedgerQueryService`` read-model branches.

The existing HTTP tests exercise the string-scoped happy paths; these tests
drive the ``RequestContext`` scope branches (ledger_id-only and legacy-subject
fallback) plus the remaining list filters directly against the service.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import (
    CategoryBudget,
    Direction,
    LedgerEntry,
    LedgerEntryRevision,
    PendingCommand,
    PendingStatus,
)
from lark_ledger.services.web_ledger import WebLedgerQueryService


async def _entry(
    session: AsyncSession,
    short_id: str,
    *,
    user: str = "ou_a",
    amount: str = "10",
    direction: Direction = Direction.EXPENSE,
    category: str = "??",
    source_type: str = "text",
    days_ago: int = 0,
    ledger_id=None,
) -> LedgerEntry:
    occurred_at = datetime(2026, 8, 8, 4, tzinfo=UTC) - timedelta(days=days_ago)
    row = LedgerEntry(
        user_open_id=user,
        ledger_id=ledger_id,
        short_id=short_id,
        amount=Decimal(amount),
        currency="CNY",
        direction=direction,
        category=category,
        note="note",
        occurred_at=occurred_at,
        source_type=source_type,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _pending(
    ctx: RequestContext,
    code: str,
    *,
    user_open_id: str = "ou_a",
    actor_user_id=None,
) -> PendingCommand:
    return PendingCommand(
        confirmation_code=code,
        user_open_id=user_open_id,
        actor_user_id=actor_user_id if actor_user_id is not None else ctx.actor_user_id,
        ledger_id=ctx.ledger_id,
        source_event_id=f"evt_{code}",
        source_type="text",
        command_type="create_entry",
        payload_json={},
        preview_json={},
        risk_reason="batch",
        status=PendingStatus.PENDING.value,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
    )


async def test_list_entries_request_context_extra_filters(session: AsyncSession) -> None:
    ctx = RequestContext(
        actor_user_id=uuid4(),
        ledger_id=uuid4(),
        source_channel="feishu",
        channel_identity_id=None,
        external_subject_id=None,
    )
    await _entry(
        session, "CCCC1", amount="10", category="??", source_type="text", ledger_id=ctx.ledger_id
    )
    await _entry(
        session,
        "CCCC2",
        amount="20",
        category="??",
        source_type="image",
        days_ago=1,
        ledger_id=ctx.ledger_id,
    )
    await _entry(
        session,
        "CCCC3",
        amount="30",
        category="??",
        source_type="text",
        days_ago=2,
        ledger_id=ctx.ledger_id,
    )
    service = WebLedgerQueryService(session)

    page = await service.list_entries(
        ctx,
        page=1,
        page_size=10,
        end=datetime(2026, 8, 8, 5, tzinfo=UTC),
        direction=Direction.EXPENSE,
        category="??",
        source_type="text",
        amount_min=Decimal("5"),
        amount_max=Decimal("15"),
    )
    assert [item.short_id for item in page.items] == ["CCCC1"]
    assert page.total == 1


async def test_entry_detail_request_context_revision_scope(session: AsyncSession) -> None:
    ctx = RequestContext(
        actor_user_id=uuid4(),
        ledger_id=uuid4(),
        source_channel="feishu",
        channel_identity_id=None,
        external_subject_id=None,
    )
    entry = await _entry(session, "EEEE1", amount="50", ledger_id=ctx.ledger_id)
    session.add(
        LedgerEntryRevision(
            entry_id=entry.id,
            user_open_id="ou_a",
            ledger_id=ctx.ledger_id,
            short_id=entry.short_id,
            change_type="created",
            before_json={},
            after_json={},
        )
    )
    await session.commit()
    service = WebLedgerQueryService(session)
    detail = await service.entry_detail(ctx, "#eeee1")
    assert detail is not None
    assert detail.entry.amount == Decimal("50")
    assert detail.revisions[0].change_type == "created"


async def test_dashboard_request_context_ledger_only_scopes(session: AsyncSession) -> None:
    ctx = RequestContext(
        actor_user_id=uuid4(),
        ledger_id=uuid4(),
        source_channel="feishu",
        channel_identity_id=None,
        external_subject_id=None,
    )
    await _entry(
        session,
        "DDDD1",
        amount="100",
        direction=Direction.INCOME,
        category="??",
        ledger_id=ctx.ledger_id,
    )
    await _entry(session, "DDDD2", amount="40", category="??", ledger_id=ctx.ledger_id)
    session.add(
        CategoryBudget(
            user_open_id="ou_a",
            ledger_id=ctx.ledger_id,
            category="??",
            amount=Decimal("200"),
        )
    )
    session.add(_pending(ctx, "CA1111"))
    await session.commit()

    service = WebLedgerQueryService(session)
    dashboard = await service.dashboard(ctx, now=datetime(2026, 8, 8, 8, tzinfo=UTC))
    assert dashboard.month_income == Decimal("100")
    assert dashboard.month_expense == Decimal("40")
    assert dashboard.pending_count == 1
    assert dashboard.budget_usage_rate == Decimal("20")


async def test_dashboard_request_context_legacy_subject_scopes(session: AsyncSession) -> None:
    ctx = RequestContext(
        actor_user_id=uuid4(),
        ledger_id=uuid4(),
        source_channel="feishu",
        channel_identity_id=None,
        external_subject_id="ou_legacy",
    )
    await _entry(
        session, "DDDD3", amount="100", direction=Direction.INCOME, category="??", user="ou_legacy"
    )
    await _entry(session, "DDDD4", amount="40", category="??", user="ou_legacy")
    session.add(
        CategoryBudget(
            user_open_id="ou_legacy",
            category="??",
            amount=Decimal("200"),
        )
    )
    session.add(
        _pending(ctx, "CA2222", user_open_id="ou_legacy", actor_user_id=None)
    )
    await session.commit()

    service = WebLedgerQueryService(session)
    dashboard = await service.dashboard(ctx, now=datetime(2026, 8, 8, 8, tzinfo=UTC))
    assert dashboard.month_income == Decimal("100")
    assert dashboard.month_expense == Decimal("40")
    assert dashboard.pending_count == 1
    assert dashboard.budget_usage_rate == Decimal("20")
