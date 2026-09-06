from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lark_ledger.action_intent import (
    ActionIntent,
    ActionIntentRegistry,
    ActionIntentRisk,
    ConfirmationPolicy,
    UnknownActionIntent,
)
from lark_ledger.context import RequestContext
from lark_ledger.models import Direction
from lark_ledger.schemas import Action, ParsedCommand
from lark_ledger.services.action_intent import ActionIntentBridge


def context() -> RequestContext:
    return RequestContext(
        actor_user_id=uuid4(), ledger_id=uuid4(), source_channel="web"
    )


def create_command() -> ParsedCommand:
    return ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("12.00"),
        direction=Direction.EXPENSE,
        category="餐饮",
        note="午饭",
        occurred_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )


def test_registry_has_read_write_allowlist_and_router_confirmation() -> None:
    descriptors = {item.action for item in ActionIntentRegistry.descriptors()}
    assert Action.CREATE in descriptors
    assert Action.QUERY in descriptors
    create = ActionIntentRegistry.resolve(ActionIntent.from_command(create_command()), context())
    assert create.risk is ActionIntentRisk.WRITE
    assert create.confirmation is ConfirmationPolicy.ROUTER


def test_unknown_or_mismatched_operations_are_rejected() -> None:
    with pytest.raises(UnknownActionIntent):
        ActionIntentRegistry.resolve(
            ActionIntent.model_construct(
                operation="not_registered", command=create_command(), resource_refs=[]
            ),
            context(),
        )
    with pytest.raises(ValidationError):
        ActionIntent(operation="query", command=create_command())


def test_action_intent_rejects_arbitrary_fields() -> None:
    with pytest.raises(ValidationError):
        ActionIntent.model_validate(
            {"operation": "create", "command": create_command().model_dump(), "sql": "select 1"}
        )


def test_bridge_only_validates_and_returns_existing_command() -> None:
    intent, descriptor = ActionIntentBridge().validate(create_command(), context())
    assert intent.command.action is Action.CREATE
    assert descriptor.action is Action.CREATE
