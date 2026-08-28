"""Safe bridge from parsed assistant intent to existing application services."""

from __future__ import annotations

from lark_ledger.action_intent import (
    ActionIntent,
    ActionIntentDescriptor,
    ActionIntentRegistry,
)
from lark_ledger.context import RequestContext
from lark_ledger.schemas import ParsedCommand


class ActionIntentBridge:
    """Validate only; execution remains in ``UnifiedAIEntryService``."""

    def validate(
        self, command: ParsedCommand, context: RequestContext
    ) -> tuple[ActionIntent, ActionIntentDescriptor]:
        intent = ActionIntent.from_command(command)
        return intent, ActionIntentRegistry.resolve(intent, context)
