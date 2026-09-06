"""Strict, transport-neutral Action Intent contracts (P52).

An Action Intent is an allowlisted proposal, not an executable tool call. The
registry below deliberately maps to the existing ``Action`` enum and leaves
resource resolution, authorization, risk routing, confirmation, idempotency,
and audit to the existing application services.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lark_ledger.context import RequestContext
from lark_ledger.schemas import AI_QUERY_ACTIONS, AI_WRITE_ACTIONS, Action, ParsedCommand


class ActionIntentRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    HIGH = "high"


class ConfirmationPolicy(StrEnum):
    NEVER = "never"
    ROUTER = "risk_router"
    ALWAYS = "always"


class ActionIntent(BaseModel):
    """A bounded proposal which must be resolved by the registry."""

    model_config = ConfigDict(extra="forbid")

    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    command: ParsedCommand
    resource_refs: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def operation_matches_command(self) -> ActionIntent:
        if self.operation != self.command.action.value:
            raise ValueError("action intent operation does not match its command")
        return self

    @classmethod
    def from_command(cls, command: ParsedCommand) -> ActionIntent:
        return cls(operation=command.action.value, command=command)


@dataclass(frozen=True, slots=True)
class ActionIntentDescriptor:
    operation: str
    action: Action
    risk: ActionIntentRisk
    confirmation: ConfirmationPolicy


class UnknownActionIntent(ValueError):
    """Raised before an assistant proposal can reach an application service."""


class ActionIntentRegistry:
    """The single allowlist for assistant-to-application proposals."""

    _DESCRIPTORS: ClassVar[dict[str, ActionIntentDescriptor]] = {
        action.value: ActionIntentDescriptor(
            operation=action.value,
            action=action,
            risk=(
                ActionIntentRisk.HIGH
                if action
                in {Action.TRANSFER, Action.BATCH, Action.CREATE_ENTRIES, Action.SET_BUDGETS}
                else ActionIntentRisk.WRITE
            ),
            confirmation=ConfirmationPolicy.ROUTER,
        )
        for action in AI_WRITE_ACTIONS
    }
    _DESCRIPTORS.update(
        {
            action.value: ActionIntentDescriptor(
                operation=action.value,
                action=action,
                risk=ActionIntentRisk.READ,
                confirmation=ConfirmationPolicy.NEVER,
            )
            for action in AI_QUERY_ACTIONS | {Action.HELP}
        }
    )

    @classmethod
    def resolve(
        cls, intent: ActionIntent, context: RequestContext
    ) -> ActionIntentDescriptor:
        descriptor = cls._DESCRIPTORS.get(intent.operation)
        if descriptor is None or descriptor.action is not intent.command.action:
            raise UnknownActionIntent("未知操作，未执行任何账本写入。")
        if context.actor_user_id is None or context.ledger_id is None:
            raise UnknownActionIntent("缺少有效的操作范围，未执行任何账本写入。")
        return descriptor

    @classmethod
    def descriptors(cls) -> tuple[ActionIntentDescriptor, ...]:
        return tuple(cls._DESCRIPTORS.values())
