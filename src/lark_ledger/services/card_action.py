"""Feishu interactive-card confirmation actions (P07).

Preview cards carry 确认 / 取消 buttons with
``value = {"k": "larkledger_pending", "action": ..., "code": ...}``. The
``card.action.trigger`` callback verifies the marker and re-checks the pending
owner / status / expiry through the pending store's row-lock; idempotency and
expiry are handled there, so a double click never re-runs business. Text
commands (确认/取消 #C-XXXXX) remain the fallback when cards are unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from lark_ledger.config import Settings
from lark_ledger.confirmation_id import (
    CONFIRMATION_PREFIX,
    ConfirmationCodeError,
    normalize_confirmation_code,
)
from lark_ledger.models import ReplyOutbox
from lark_ledger.services.pending import CARD_ACTION_KEY, PendingCommandStore

logger = logging.getLogger(__name__)

_CARD_ACTIONS = frozenset({"confirm", "cancel"})


class CardActionService:
    """Validate and process one card button action."""

    def __init__(
        self,
        settings: Settings,
        pending_store: PendingCommandStore,
        exchange_rates: Any,
        deliverer: Callable[[list[ReplyOutbox]], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._pending_store = pending_store
        self._exchange_rates = exchange_rates
        self._deliverer = deliverer

    async def handle_action(
        self, event_id: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        """Process one ``card.action.trigger`` event and return the Feishu response.

        The reply is delivered through the outbox (Reply Worker or sync path),
        so a delivery failure never re-runs the confirmation business.
        """
        operator = event.get("operator") or {}
        operator_open_id = str(
            operator.get("open_id") or operator.get("user_id") or ""
        )
        if not operator_open_id:
            return {"toast": {"type": "error", "content": "无法识别操作者"}}

        value = event.get("action", {}).get("value") or {}
        action = value.get("action")
        code_suffix = str(value.get("code") or "")
        if value.get("k") != CARD_ACTION_KEY or action not in _CARD_ACTIONS:
            return {"toast": {"type": "error", "content": "无效操作"}}
        try:
            confirmation_code = normalize_confirmation_code(
                f"{CONFIRMATION_PREFIX}-{code_suffix}"
            )
        except ConfirmationCodeError:
            return {"toast": {"type": "error", "content": "无效确认单"}}

        context = event.get("context") or {}
        reply_to_message_id = str(
            context.get("open_message_id") or context.get("message_id") or ""
        )
        if not reply_to_message_id:
            return {"toast": {"type": "error", "content": "缺少回复目标"}}

        now = datetime.now(UTC)
        # No confirm_event_id: card actions are not stored events, so the reply
        # outbox row is event-less and idempotency comes from the pending row
        # status under the row lock (a re-click returns the idempotent message).
        if action == "confirm":
            _, rows = await self._pending_store.confirm_and_execute(
                user_open_id=operator_open_id,
                confirmation_code=confirmation_code,
                reply_to_message_id=reply_to_message_id,
                confirm_event_id=None,
                exchange_rates=self._exchange_rates,
                now=now,
            )
        else:
            _, rows = await self._pending_store.cancel(
                user_open_id=operator_open_id,
                confirmation_code=confirmation_code,
                reply_to_message_id=reply_to_message_id,
                cancel_event_id=None,
                now=now,
            )
        await self._deliverer(rows)
        return {"toast": {"type": "success", "content": "已处理"}}
