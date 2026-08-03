from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from lark_ledger.models import Direction
from lark_ledger.schemas import Action, ParsedCommand


def test_create_command_requires_business_fields() -> None:
    command = ParsedCommand(
        action=Action.CREATE,
        amount=Decimal("9"),
        direction=Direction.EXPENSE,
        category="餐饮",
        occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert command.amount == Decimal("9")


def test_create_command_rejects_missing_amount() -> None:
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.CREATE,
            direction=Direction.EXPENSE,
            category="餐饮",
            occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
        )


def test_command_rejects_unknown_sql_field() -> None:
    with pytest.raises(ValidationError):
        ParsedCommand.model_validate({"action": "help", "sql": "DROP TABLE ledger_entries"})


def test_report_command_requires_valid_range() -> None:
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.REPORT)

    command = ParsedCommand(
        action=Action.REPORT,
        range_start=datetime(2026, 8, 1, tzinfo=UTC),
        range_end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert command.action is Action.REPORT


def test_budget_commands_require_expected_fields() -> None:
    command = ParsedCommand(action=Action.SET_BUDGET, category="餐饮", amount=Decimal("1500"))
    assert command.amount == Decimal("1500")
    assert ParsedCommand(action=Action.LIST_BUDGETS, category="餐饮").category == "餐饮"

    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.SET_BUDGET, category="餐饮")
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.DELETE_BUDGET)
