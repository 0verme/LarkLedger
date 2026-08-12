"""P39 — Unified AI Entry: channel-neutral AI command pipeline.

Every channel adapter (Feishu message events, the First-party Web client, and
future WeChat / hardware adapters) feeds the same canonical AI input into this
service:

    Channel Input → AIEntryRequest → UnifiedAIEntryService
        → AIInterpreter (Intent Parser) → ParsedCommand (Canonical Intent)
        → RiskRouter (decision only, never writes)
        → ClientApplicationService (execute) / PendingCommand (confirm)
        → AIEntryResult (Canonical Result)

The service never imports Feishu, FastAPI or the Web client, never touches the
database directly (only through domain services) and never executes SQL.
``source_channel`` travels inside the ``RequestContext`` and is observability /
presentation metadata — it never changes business semantics, permissions or
domain behavior (P39 §6/§22). Transport objects (Feishu events, message ids,
cards, HTTP responses) never enter the application layer; adapters exchange
them for neutral references such as ``source_message_ref``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.entry_commands import bind_entry_refs_from_message
from lark_ledger.models import PendingCommand
from lark_ledger.schemas import (
    AI_QUERY_ACTIONS,
    Action,
    AIEntryResult,
    AIEntryStatus,
    ExecutionResult,
    ParsedCommand,
)
from lark_ledger.services.ai import AIInterpreter, CommandInterpretationError
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError
from lark_ledger.services.ledger import HELP_TEXT, LedgerAccessDeniedError
from lark_ledger.services.ledger_authorization import LedgerAuthorizationError
from lark_ledger.services.member_resolution import PayerResolutionError
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.services.risk import MediaKind, RiskAssessment, RiskDecision, RiskRouter
from lark_ledger.services.transfers import AccountHintAmbiguousError

logger = logging.getLogger(__name__)


class AIEntryParseError(ValueError):
    """An interpreted command could not be turned into a safe ParsedCommand
    (e.g. an entry reference not present in the user message). Nothing was
    written; the caller decides how to present it (clarification or a
    controlled rejection)."""


@dataclass(frozen=True, slots=True)
class AttachmentInput:
    """Canonical attachment contract (P39 §8).

    The adapter downloads / normalizes transport resources into this neutral
    shape; the AI core never calls a channel API (no Feishu download, no HTTP
    resource fetch).
    """

    kind: Literal["image", "audio"]
    mime_type: str | None = None
    content: bytes | None = None
    storage_ref: str | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class AIEntryRequest:
    """Channel-neutral input to the Unified AI Entry (P39 §6/§7).

    ``context`` carries the resolved actor + ledger scope (``source_channel``
    inside is observability metadata). ``source_message_ref`` is a neutral
    tracing reference — never a Feishu message id object or an HTTP request.
    """

    context: RequestContext
    text: str
    request_id: str
    attachments: tuple[AttachmentInput, ...] = ()
    source_message_ref: str | None = None
    now: datetime | None = None
    media: MediaKind = MediaKind.NONE


class UnifiedAIEntryService:
    """Channel-neutral AI command pipeline shared by every adapter (P39).

    Adapters may call the granular methods (``parse`` / ``decide`` /
    ``create_pending`` / ``execute``) to keep their own transaction / outbox
    shape (Feishu), or the one-shot ``submit`` pipeline (Web). Both paths share
    the same intent parser, risk rules and application boundary.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        interpreter: AIInterpreter | None = None,
        exchange_rates: ExchangeRateService | None = None,
    ) -> None:
        self.settings = settings
        self._factory = session_factory
        self._interpreter = interpreter or AIInterpreter(settings)
        self._exchange_rates = exchange_rates or ExchangeRateService(settings)
        self._risk_router = RiskRouter(session_factory, settings)
        self._pending_store = PendingCommandStore(session_factory, settings)

    # ------------------------------------------------------------------
    # Granular pipeline steps (Feishu keeps its outbox transaction intact)
    # ------------------------------------------------------------------

    async def parse(
        self,
        *,
        text: str,
        now: datetime,
        images: Sequence[bytes] | None = None,
        source_message_ref: str | None = None,
    ) -> ParsedCommand:
        """AI interpretation + entry-reference binding (channel-neutral).

        Raises ``CommandInterpretationError`` when the model output fails
        schema validation and ``AIEntryParseError`` when the message cannot be
        safely bound (e.g. an invented short ID). Nothing is written.
        """
        del source_message_ref  # reserved for neutral tracing metadata
        command = await self._interpreter.interpret(text, now=now, images=images)
        bound = bind_entry_refs_from_message(command, text)
        if isinstance(bound, str):
            raise AIEntryParseError(bound)
        return bound

    async def decide(
        self,
        *,
        command: ParsedCommand,
        source_type: str,
        user_open_id: str,
        media: MediaKind = MediaKind.NONE,
        context: RequestContext | None = None,
        session: AsyncSession | None = None,
    ) -> RiskAssessment:
        """Risk decision only — the router never writes.

        ``session`` is the caller's active session (used by the Web idempotency
        pipeline so the duplicate probe shares one SQLite/Postgres connection
        with the pending/execute work); Feishu keeps its own factory session.
        """
        return await self._risk_router.route(
            command=command,
            source_type=source_type,
            user_open_id=user_open_id,
            media=media,
            context=context,
            session=session,
        )

    async def execute(
        self,
        *,
        session: AsyncSession,
        context: RequestContext,
        command: ParsedCommand,
        source_type: str,
        source_message_id: str | None = None,
        commit_changes: bool = True,
    ) -> ExecutionResult:
        """Execute a canonical intent through the shared application boundary
        (``ClientApplicationService``) — never a raw repository / SQL path."""
        application = ClientApplicationService(
            session,
            currency=self.settings.currency,
            timezone=self.settings.timezone,
            exchange_rates=self._exchange_rates,
        )
        return await application.execute_financial(
            context,
            command,
            source_type=source_type,
            source_message_id=source_message_id,
            commit_changes=commit_changes,
        )

    async def create_pending(
        self,
        *,
        session: AsyncSession,
        context: RequestContext,
        command: ParsedCommand,
        risk: RiskAssessment,
        source_type: str,
        source_message_id: str,
        user_open_id: str,
        transport: str,
        source_event_id: str | None = None,
        source_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> PendingCommand:
        """Freeze a high-risk command for user confirmation (P39 §26/§27).

        Adds the pending row to ``session``; the caller commits it (Feishu
        together with the preview outbox card, Web inside the idempotency
        record transaction) so confirm exactly-once semantics are preserved.
        """
        return await self._pending_store.create_pending(
            session=session,
            event_id=source_event_id,
            message_id=source_message_id,
            source_fingerprint=source_fingerprint,
            user_open_id=user_open_id,
            command=command,
            source_type=source_type,
            risk=risk,
            now=now or datetime.now(UTC),
            context=context,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # One-shot pipeline (stateless adapters — Web AI entry)
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        session: AsyncSession,
        request: AIEntryRequest,
        commit_changes: bool = True,
    ) -> AIEntryResult:
        """Full channel-neutral pipeline: parse → decide → execute / pending.

        Used by stateless adapters (the Web AI entry). Feishu keeps its own
        outbox transaction via the granular methods; both share the same
        intent parser, risk rules and application boundary.
        """
        now = request.now or datetime.now(ZoneInfo(self.settings.timezone))
        images = [a.content for a in request.attachments if a.kind == "image" and a.content]
        try:
            command = await self.parse(
                text=request.text,
                now=now,
                images=images,
                source_message_ref=request.source_message_ref,
            )
        except CommandInterpretationError:
            return AIEntryResult(
                status=AIEntryStatus.CLARIFICATION_REQUIRED,
                message=(
                    "没有完整识别这句话，本次没有写入账本。"
                    "请明确写出用途、金额和收支方向，例如：午饭28、工资18000。"
                ),
                request_id=request.request_id,
            )
        except AIEntryParseError as exc:
            return AIEntryResult(
                status=AIEntryStatus.CLARIFICATION_REQUIRED,
                message=str(exc),
                request_id=request.request_id,
            )
        except Exception as exc:  # noqa: BLE001 - provider timeout / network
            # P39 §58/§64: an AI provider failure must never produce a
            # half-written ledger row (parse happens before any mutation) and
            # surfaces as a safe error envelope with the request id.
            logger.exception(
                "unified AI entry provider failure request_id=%s error_type=%s",
                request.request_id,
                type(exc).__name__,
            )
            return AIEntryResult(
                status=AIEntryStatus.ERROR,
                message="AI 服务暂时不可用，请稍后再试。",
                request_id=request.request_id,
            )

        if command.action is Action.HELP:
            return AIEntryResult(
                status=AIEntryStatus.CLARIFICATION_REQUIRED,
                message=HELP_TEXT,
                request_id=request.request_id,
            )

        actor_ref = request.context.external_subject_id or str(request.context.actor_user_id)
        source_message_id = request.source_message_ref or request.request_id
        source_type = request.context.source_channel
        if self.settings.pending_enabled:
            risk = await self.decide(
                command=command,
                source_type=source_type,
                user_open_id=actor_ref,
                media=request.media,
                context=request.context,
                session=session,
            )
            if risk.decision is RiskDecision.REJECT:
                return AIEntryResult(
                    status=AIEntryStatus.REJECTED,
                    message=risk.message or "该请求被拒绝，未写入账本。",
                    request_id=request.request_id,
                )
            if risk.decision is RiskDecision.PENDING:
                pending = await self.create_pending(
                    session=session,
                    context=request.context,
                    command=command,
                    risk=risk,
                    source_type=source_type,
                    source_message_id=source_message_id,
                    user_open_id=actor_ref,
                    transport=request.context.source_channel,
                    now=now,
                )
                if commit_changes:
                    await session.commit()
                logger.info(
                    "unified AI entry pending created request_id=%s confirmation_code=%s "
                    "channel=%s risk_reason=%s",
                    request.request_id,
                    pending.confirmation_code,
                    request.context.source_channel,
                    risk.reason.value if risk.reason else "high_risk",
                )
                return AIEntryResult(
                    status=AIEntryStatus.CONFIRMATION_REQUIRED,
                    message="这项操作需要确认。",
                    request_id=request.request_id,
                    pending_command_id=pending.confirmation_code,
                    confirmation_code=pending.confirmation_code,
                    risk=risk.reason.value if risk.reason else "high_risk",
                    expires_at=pending.expires_at,
                    preview=json.loads(pending.preview_json)
                    if isinstance(pending.preview_json, str)
                    else dict(pending.preview_json),
                )

        try:
            result = await self.execute(
                session=session,
                context=request.context,
                command=command,
                source_type=source_type,
                source_message_id=source_message_id,
                commit_changes=False,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a safe canonical result
            # Execution failed: no half-written ledger row may survive. The
            # idempotency record is also rolled back, so a retry with the same
            # key re-runs cleanly (nothing was persisted).
            await session.rollback()
            return AIEntryResult(
                status=AIEntryStatus.ERROR,
                message=self._safe_execution_message(exc, request.request_id),
                request_id=request.request_id,
            )
        if commit_changes:
            await session.commit()
        return self._result_for(request, command, result)

    def _safe_execution_message(self, exc: Exception, request_id: str) -> str:
        """Map domain failures to safe user-facing Chinese (P39 §64/§65).

        Known business errors keep their precise wording; anything unexpected
        becomes a generic safe message with the request id for support.
        """
        if isinstance(exc, (LedgerAccessDeniedError, LedgerAuthorizationError)):
            return "账本不可访问，请检查当前账本后重试。"
        if isinstance(exc, AccountHintAmbiguousError):
            return (
                "账户名称不明确、已归档或不属于当前账本，本次未执行任何写入。"
                "请使用准确的账户名称后重试，例如：用招商银行记支出。"
            )
        if isinstance(exc, PayerResolutionError):
            return str(exc)
        if isinstance(exc, ExchangeRateUnavailableError):
            return "暂时无法获取汇率，请稍后重试。"
        if isinstance(exc, ValueError):
            return str(exc)
        logger.exception(
            "unified AI entry execution failed request_id=%s error_type=%s",
            request_id,
            type(exc).__name__,
        )
        return "这笔操作暂时无法完成，请稍后重试或换一种说法。"

    def _result_for(
        self, request: AIEntryRequest, command: ParsedCommand, result: ExecutionResult
    ) -> AIEntryResult:
        if command.action in AI_QUERY_ACTIONS:
            return AIEntryResult(
                status=AIEntryStatus.QUERY_RESULT,
                message=result.message,
                request_id=request.request_id,
                operation=command.action.value,
            )
        return AIEntryResult(
            status=AIEntryStatus.EXECUTED,
            message=result.message,
            request_id=request.request_id,
            operation=command.action.value,
            resource_id=str(result.entry_id) if result.entry_id is not None else None,
            amount=str(command.amount) if command.amount is not None else None,
            direction=command.direction.value if command.direction is not None else None,
            category=command.category,
            account=command.account_hint,
            occurred_at=command.occurred_at,
        )
