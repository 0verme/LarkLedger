from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lark_ledger.models import Direction


class Action(StrEnum):
    CREATE = "create"
    UPDATE_LAST = "update_last"
    UNDO_LAST = "undo_last"
    SUMMARY = "summary"
    HELP = "help"


class ParsedCommand(BaseModel):
    """Only data the AI may return; there is intentionally no SQL-shaped field."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    direction: Direction | None = None
    category: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ParsedCommand":
        if self.action is Action.CREATE:
            missing = [
                name
                for name in ("amount", "direction", "category", "occurred_at")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"create is missing: {', '.join(missing)}")
        if self.action is Action.SUMMARY:
            if self.range_start is None or self.range_end is None:
                raise ValueError("summary requires range_start and range_end")
            if self.range_start >= self.range_end:
                raise ValueError("summary range must be increasing")
        if self.action is Action.UPDATE_LAST and all(
            value is None
            for value in (self.amount, self.direction, self.category, self.note, self.occurred_at)
        ):
            raise ValueError("update_last requires at least one changed field")
        return self


class SummaryItem(BaseModel):
    category: str
    amount: Decimal


class ExecutionResult(BaseModel):
    message: str
