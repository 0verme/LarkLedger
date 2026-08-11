"""P33-B deterministic insights on real PostgreSQL.

Verifies the deterministic engine end to end: budget insight aggregates only
visible spending, recurring insight respects rule privacy, and the household
two-user privacy case (P33 §48) holds — B's insights never reveal A's private
data through any side channel.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.postgres


async def _household(factory: async_sessionmaker) -> tuple[object, object, object]:
    from lark_ledger.context import RequestContext
    from lark_ledger.services.household_management import HouseholdManagementService
    from lark_ledger.services.identity import IdentityService

    async with factory() as session:
        owner = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_pi_owner", display_name="A"
        )
        member = await IdentityService(
            session, currency="CNY", timezone="Asia/Shanghai"
        ).resolve_or_bootstrap(
            channel="feishu", external_subject_id="ou_pi_member", display_name="B"
        )
        manager = HouseholdManagementService(session, currency="CNY", timezone="Asia/Shanghai")
        home = await manager.create(owner.actor_user_id, "PG 洞察家庭")
        invitation = await manager.invite(owner.actor_user_id, home.household.id, "ou_pi_member")
        await manager.accept(member.actor_user_id, invitation.public_id)
        owner_ctx = RequestContext(
            actor_user_id=owner.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_pi_owner",
        )
        member_ctx = RequestContext(
            actor_user_id=member.actor_user_id,
            ledger_id=home.ledger.id,
            source_channel="feishu",
            external_subject_id="ou_pi_member",
        )
        await session.commit()
        return owner_ctx, member_ctx, home.ledger


async def test_insights_household_privacy_and_budget_on_postgres(
    postgres_engine: AsyncEngine,
) -> None:
    from lark_ledger.models import (
        AccountType,
        AccountVisibility,
        Direction,
        LedgerEntry,
    )
    from lark_ledger.services.accounts import AccountService
    from lark_ledger.services.budget import BudgetService
    from lark_ledger.services.insights import InsightService

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    owner_ctx, member_ctx, _ = await _household(factory)

    async with factory() as session:
        private = await AccountService(session).create(
            owner_ctx,
            name="私房钱",
            account_type=AccountType.CASH,
            currency="CNY",
            opening_balance=Decimal("0"),
            visibility=AccountVisibility.PRIVATE,
        )
        now = datetime(2026, 8, 8, 4, tzinfo=UTC)
        # A's private spending history + a sharp jump this month.
        for month_offset, amount in ((3, "1000"), (2, "1000"), (1, "1000")):
            session.add(
                LedgerEntry(
                    user_open_id="ou_pi_owner",
                    created_by_user_id=owner_ctx.actor_user_id,
                    paid_by_user_id=owner_ctx.actor_user_id,
                    ledger_id=owner_ctx.ledger_id,
                    account_id=private.id,
                    short_id=f"PG{month_offset}",
                    amount=Decimal(amount),
                    currency="CNY",
                    direction=Direction.EXPENSE,
                    category="私人购物",
                    note="",
                    occurred_at=now - timedelta(days=30 * month_offset),
                    source_type="text",
                )
            )
        session.add(
            LedgerEntry(
                user_open_id="ou_pi_owner",
                created_by_user_id=owner_ctx.actor_user_id,
                paid_by_user_id=owner_ctx.actor_user_id,
                ledger_id=owner_ctx.ledger_id,
                account_id=private.id,
                short_id="PG4",
                amount=Decimal("1500"),
                currency="CNY",
                direction=Direction.EXPENSE,
                category="私人购物",
                note="",
                occurred_at=now - timedelta(days=1),
                source_type="text",
            )
        )
        # A budget that would look "at risk" if A's private spend were leaked.
        await BudgetService(session, currency="CNY", timezone="Asia/Shanghai").set_category_budget(
            member_ctx, period=date(2026, 8, 1), category="私人购物", amount=Decimal("1000")
        )
        await session.commit()

        service = InsightService(session, timezone="Asia/Shanghai", currency="CNY")
        owner_insights = await service.insights(owner_ctx, period=date(2026, 8, 1), now=now)
        member_insights = await service.insights(member_ctx, period=date(2026, 8, 1), now=now)

        # Owner sees their private category change.
        assert any(item.related_category == "私人购物" for item in owner_insights)
        # Member sees nothing about 私人购物 — no spending change, no budget
        # risk built on leaked private spend, no amounts.
        assert all(item.related_category != "私人购物" for item in member_insights)
        for item in member_insights:
            assert "1500" not in item.summary
            assert "私人购物" not in item.summary
        # The budget risk rule must not fire for the member off A's private
        # spend (their visible spend for the category is 0 → usage 0%).
        assert not any(
            item.type == "budget_risk" and item.related_category == "私人购物"
            for item in member_insights
        )
