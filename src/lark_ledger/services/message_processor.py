"""Message processing pipeline for incoming Feishu events (split from ``feishu.py``)."""

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.account_commands import try_parse_account_command
from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.entry_commands import (
    PendingDirective,
    bind_entry_refs_from_message,
    try_parse_deterministic_entry_command,
    try_parse_pending_directive,
)
from lark_ledger.household_commands import (
    HouseholdCommand,
    HouseholdCommandAction,
    try_parse_household_command,
)
from lark_ledger.ledger_commands import (
    LedgerCommand,
    LedgerCommandAction,
    try_parse_ledger_command,
)
from lark_ledger.models import (
    Direction,
    Household,
    Ledger,
    ProcessedEvent,
    RecurringRule,
    ReplyOutbox,
    User,
)
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_card_payload,
    build_file_payload,
    build_text_payload,
)
from lark_ledger.recurring_commands import (
    RecurringCommand,
    RecurringCommandAction,
    try_parse_recurring_command,
)
from lark_ledger.schemas import (
    MAX_BATCH_BUDGETS,
    MAX_BATCH_ENTRIES,
    Action,
    ExecutionResult,
    ParsedCommand,
)
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.ai import AIInterpreter, CommandInterpretationError
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError
from lark_ledger.services.feishu_client import FeishuClient, _media_fingerprint, logger
from lark_ledger.services.household_management import (
    HouseholdManagementError,
)
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger_management import LedgerManagementError
from lark_ledger.services.outbox import ReplyOutboxStore
from lark_ledger.services.pending import (
    PendingCommandStore,
    PendingPreview,
    build_pending_preview_card,
)
from lark_ledger.services.recurring import (
    RecurringRuleConflictError,
    RecurringRuleError,
    RecurringRuleNotFoundError,
)
from lark_ledger.services.reply_worker import ReplyDeliverer
from lark_ledger.services.report import ReportRenderer, build_report_card, fallback_advice
from lark_ledger.services.risk import MediaKind, RiskAssessment, RiskDecision, RiskRouter
from lark_ledger.services.transfers import AccountHintAmbiguousError
from lark_ledger.services.worker import generate_owner_id
from lark_ledger.transfer_commands import try_parse_transfer_command

MAX_POST_IMAGES = 5


class MessageProcessor:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        feishu: FeishuClient,
        interpreter: AIInterpreter,
        renderer: ReportRenderer | None = None,
        exchange_rates: ExchangeRateService | None = None,
        outbox_store: ReplyOutboxStore | None = None,
        reply_deliverer: ReplyDeliverer | None = None,
        reply_worker_enabled: bool = False,
        wakeup: Callable[[], None] | None = None,
    ) -> None:
        """``reply_worker_enabled=True``: after committing business + outbox the
        processor only signals the background Reply Worker (``wakeup``) and
        returns; delivery belongs to the worker. ``False`` (the default, and the
        value used by every unit test): the processor runs the compatible
        synchronous path — claim each freshly committed row with the same
        lease-guarded primitives the worker uses, deliver, and mark — so no
        send path bypasses the outbox guards.
        """
        self.settings = settings
        self.session_factory = session_factory
        self.feishu = feishu
        self.interpreter = interpreter
        self.renderer = renderer or ReportRenderer(settings.report_font_path)
        self.exchange_rates = exchange_rates or ExchangeRateService(settings)
        self.outbox_store = outbox_store or ReplyOutboxStore(session_factory)
        self._risk_router = RiskRouter(session_factory, settings)
        self._pending_store = PendingCommandStore(session_factory, settings)
        self._reply_worker_enabled = reply_worker_enabled
        self._wakeup = wakeup
        self._sync_owner = generate_owner_id()
        self._reply_deliverer = reply_deliverer or ReplyDeliverer(
            self.outbox_store,
            feishu,
            owner_id=self._sync_owner,
            max_attempts=settings.reply_max_attempts,
            retry_base_seconds=settings.reply_retry_base_seconds,
            retry_max_seconds=settings.reply_retry_max_seconds,
            jitter=None,
        )

    async def process(self, event: dict[str, Any]) -> None:
        message = event["message"]
        message_id = str(message["message_id"])
        event_id = str(event.get("event_id") or "") or None
        sender = event.get("sender", {}).get("sender_id", {})
        user_open_id = str(sender.get("open_id") or sender.get("user_id") or "")
        if not user_open_id:
            return
        message_type = str(message.get("message_type", ""))
        command: ParsedCommand | None = None
        fingerprint_ledger_id: uuid.UUID | None = None
        stage = "message_decode"
        is_visual_message = message_type == "image"

        # Crash-window recovery (P06a): a ``reply_outbox`` row exists only if the
        # business transaction it was written with committed. When an event is
        # re-delivered after a crash between that commit and the event status
        # update, skip business entirely (no duplicate entries / outbox rows)
        # and let the worker converge the event to ``succeeded``.
        if event_id is not None and (
            await self.outbox_store.has_outbox(event_id)
            or await self._business_result_committed(event_id)
        ):
            logger.info(
                "event already processed; skipping business event_id=%s message_id=%s",
                event_id,
                message_id,
            )
            return
        try:
            content = json.loads(message.get("content", "{}"))
            now = datetime.now(ZoneInfo(self.settings.timezone))
            images: list[bytes] = []
            text = ""
            source_type = message_type
            if message_type == "text":
                text = str(content.get("text", "")).strip()
            elif message_type == "image":
                if not self.interpreter.vision_configured:
                    await self.feishu.reply_text(message_id, "图片识别功能尚未配置。")
                    return
                stage = "image_download"
                images = [
                    await self.feishu.download_resource(
                        message_id, str(content["image_key"]), "image"
                    )
                ]
                text = "识别这张小票或支付截图并记账"
            elif message_type == "post":
                text, image_keys = self._parse_post_content(content)
                if len(image_keys) > MAX_POST_IMAGES:
                    await self.feishu.reply_text(
                        message_id,
                        f"一条富文本消息最多处理 {MAX_POST_IMAGES} 张图片，请拆分后重新发送。",
                    )
                    return
                if image_keys:
                    is_visual_message = True
                    if not self.interpreter.vision_configured:
                        await self.feishu.reply_text(message_id, "图片识别功能尚未配置。")
                        return
                    stage = "image_download"
                    images = list(
                        await asyncio.gather(
                            *(
                                self.feishu.download_resource(message_id, key, "image")
                                for key in image_keys
                            )
                        )
                    )
                elif not text:
                    await self.feishu.reply_text(message_id, "这条富文本中没有可识别的文字或图片。")
                    return
            elif message_type in {"audio", "file"}:
                if not self.interpreter.transcription_configured:
                    await self.feishu.reply_text(message_id, "语音识别功能尚未配置。")
                    return
                file_key = str(content.get("file_key", ""))
                if not file_key:
                    raise ValueError("消息中没有 file_key")
                stage = "audio_download"
                audio = await self.feishu.download_resource(message_id, file_key, "file")
                stage = "transcription"
                text = await self.interpreter.transcribe(
                    audio, filename=str(content.get("file_name", "voice.opus"))
                )
                source_type = "audio"
            else:
                await self.feishu.reply_text(
                    message_id, "暂时只支持文字、语音、小票照片和支付截图。"
                )
                return
            stage = "vision_interpretation" if images else "interpretation"
            source_fingerprint = _media_fingerprint(source_type, text, images) if images else None
            if (
                self.settings.pending_enabled
                and source_fingerprint is not None
                and (fingerprint_ledger_id := await self._current_ledger_id(user_open_id))
                and await self._pending_store.has_active_fingerprint(
                    fingerprint_ledger_id, source_fingerprint
                )
            ):
                await self._mark_event_business_committed(event_id)
                logger.info(
                    "duplicate active media pending suppressed event_id=%s message_id=%s",
                    event_id,
                    message_id,
                )
                return
            if not images:
                household_command = try_parse_household_command(text)
                if isinstance(household_command, str):
                    await self.feishu.reply_text(message_id, household_command)
                    return
                if household_command is not None:
                    stage = "household_management"
                    outbox_rows = await self._handle_household_command(
                        household_command,
                        message_id=message_id,
                        user_open_id=user_open_id,
                        event_id=event_id,
                    )
                    stage = "reply"
                    await self._signal_or_deliver(outbox_rows)
                    return
                ledger_command = try_parse_ledger_command(text)
                if isinstance(ledger_command, str):
                    await self.feishu.reply_text(message_id, ledger_command)
                    return
                if ledger_command is not None:
                    stage = "ledger_management"
                    outbox_rows = await self._handle_ledger_command(
                        ledger_command,
                        message_id=message_id,
                        user_open_id=user_open_id,
                        event_id=event_id,
                    )
                    stage = "reply"
                    await self._signal_or_deliver(outbox_rows)
                    return
                # Confirmation directives (P07) are deterministic and must never
                # reach the AI interpreter: 确认/取消/查看待确认 #C-A83F2.
                directive = try_parse_pending_directive(text)
                if isinstance(directive, str):
                    await self.feishu.reply_text(message_id, directive)
                    return
                if directive is not None:
                    stage = "pending_action"
                    outbox_rows = await self._handle_pending_directive(
                        directive,
                        message_id=message_id,
                        user_open_id=user_open_id,
                        event_id=event_id,
                    )
                    stage = "reply"
                    await self._signal_or_deliver(outbox_rows)
                    return
                transfer_command = try_parse_transfer_command(text, now=now)
                if isinstance(transfer_command, str):
                    await self.feishu.reply_text(message_id, transfer_command)
                    return
                if transfer_command is not None:
                    command = transfer_command
                deterministic = (
                    try_parse_deterministic_entry_command(text) if command is None else None
                )
                if isinstance(deterministic, str):
                    await self.feishu.reply_text(message_id, deterministic)
                    return
                if deterministic is not None:
                    command = deterministic
                # Recurring-rule commands (P29): create / list / pause / resume /
                # skip a 周期账单. Deterministic so recurring intents never reach
                # the AI interpreter; the short-ID entry commands above still win
                # for 恢复 #A83F2.
                recurring_command = (
                    try_parse_recurring_command(text, now=now) if command is None else None
                )
                if isinstance(recurring_command, str):
                    await self.feishu.reply_text(message_id, recurring_command)
                    return
                if recurring_command is not None:
                    stage = "recurring_management"
                    outbox_rows = await self._handle_recurring_command(
                        recurring_command,
                        message_id=message_id,
                        user_open_id=user_open_id,
                        event_id=event_id,
                        now=now,
                    )
                    stage = "reply"
                    await self._signal_or_deliver(outbox_rows)
                    return
                account_command = try_parse_account_command(text) if command is None else None
                if account_command is not None:
                    command = account_command
            if command is None:
                command = await self.interpreter.interpret(text, now=now, images=images)
                bound = bind_entry_refs_from_message(command, text)
                if isinstance(bound, str):
                    await self.feishu.reply_text(message_id, bound)
                    return
                command = bound

            # Risk routing (P07): high-risk writes (image / voice / batch /
            # likely duplicate) create a pending confirmation instead of hitting
            # the ledger; simple single text writes continue straight through.
            write_source_message_id = (
                message_id
                if command.action
                in {Action.CREATE, Action.CREATE_ENTRIES, Action.BATCH, Action.TRANSFER}
                else None
            )
            if self.settings.pending_enabled:
                media = (
                    MediaKind.VISION
                    if images
                    else MediaKind.TRANSCRIPTION
                    if source_type == "audio"
                    else MediaKind.NONE
                )
                risk = await self._risk_router.route(
                    command=command,
                    source_type=source_type,
                    user_open_id=user_open_id,
                    media=media,
                )
                if risk.decision is RiskDecision.PENDING:
                    stage = "pending_create"
                    outbox_rows = await self._create_pending_with_preview_outbox(
                        event_id=event_id,
                        message_id=message_id,
                        user_open_id=user_open_id,
                        command=command,
                        source_type=source_type,
                        source_message_id=write_source_message_id,
                        source_fingerprint=source_fingerprint,
                        risk=risk,
                    )
                    stage = "reply"
                    await self._signal_or_deliver(outbox_rows)
                    return
                if risk.decision is RiskDecision.REJECT:
                    await self.feishu.reply_text(
                        message_id, risk.message or "该请求被拒绝，未写入账本。"
                    )
                    return

            stage = "persistence"
            outbox_rows = await self._execute_with_outbox(
                event_id=event_id,
                message_id=message_id,
                user_open_id=user_open_id,
                command=command,
                source_type=source_type,
                source_message_id=write_source_message_id,
            )
            # From here the business + outbox are already committed; a post-commit
            # bookkeeping failure must not send a misleading "save failed" reply.
            stage = "reply"
            await self._signal_or_deliver(outbox_rows)
        except CommandInterpretationError:
            error_id = self._log_processing_error(stage, message_id, message_type)
            if is_visual_message:
                text = "没有完整识别图片中的交易，请裁剪无关内容或换一张更清晰的截图。"
            else:
                text = (
                    "没有完整识别这条指令，本次没有写入账本。请明确写出每笔的用途、金额和"
                    f"收支方向；一条消息最多包含{MAX_BATCH_ENTRIES}笔账目和"
                    f"{MAX_BATCH_BUDGETS}项预算，修改、撤销、查询或报告"
                    "请单独发送。"
                )
            await self.feishu.reply_text(
                message_id,
                f"{text}（错误编号：{error_id}）",
            )
        except ExchangeRateUnavailableError:
            error_id = self._log_processing_error(stage, message_id, message_type)
            await self.feishu.reply_text(
                message_id,
                f"暂时无法获取汇率，请稍后重试。（错误编号：{error_id}）",
            )
        except AccountHintAmbiguousError:
            # Controlled rejection: an account hint did not uniquely resolve to
            # an active account in the current ledger. No business was written.
            await self.feishu.reply_text(
                message_id,
                "账户名称不明确、已归档或不属于当前账本，本次未执行任何写入。"
                "请使用准确的账户名称后重试，例如：查看支付宝余额、用招商银行记支出。",
            )
        except Exception:
            error_id = self._log_processing_error(stage, message_id, message_type)
            if stage != "reply":
                await self.feishu.reply_text(
                    message_id,
                    f"{self._stage_error_message(stage)}（错误编号：{error_id}）",
                )
            raise

    @classmethod
    def _parse_post_content(cls, content: dict[str, Any]) -> tuple[str, list[str]]:
        body = content.get("content")
        if not isinstance(body, list):
            raise ValueError("富文本消息缺少 content 数组")

        lines: list[str] = []
        title = content.get("title")
        if isinstance(title, str) and title.strip():
            lines.append(title.strip())

        image_keys: list[str] = []
        seen_image_keys: set[str] = set()
        for row in body:
            if not isinstance(row, list):
                continue
            fragments: list[str] = []
            for element in row:
                cls._collect_post_element(
                    element,
                    fragments=fragments,
                    image_keys=image_keys,
                    seen_image_keys=seen_image_keys,
                )
            line = "".join(fragments).strip()
            if line:
                lines.append(line)
        return "\n".join(lines), image_keys

    @classmethod
    def _collect_post_element(
        cls,
        element: object,
        *,
        fragments: list[str],
        image_keys: list[str],
        seen_image_keys: set[str],
    ) -> None:
        if not isinstance(element, dict):
            return
        tag = element.get("tag")
        if tag in {"text", "a", "code_block"}:
            value = element.get("text")
            if isinstance(value, str):
                fragments.append(value)
            return
        if tag == "img":
            key = element.get("image_key")
            if isinstance(key, str) and key and key not in seen_image_keys:
                seen_image_keys.add(key)
                image_keys.append(key)
            return
        if tag == "note":
            nested = element.get("elements")
            if isinstance(nested, list):
                for child in nested:
                    cls._collect_post_element(
                        child,
                        fragments=fragments,
                        image_keys=image_keys,
                        seen_image_keys=seen_image_keys,
                    )

    @staticmethod
    def _log_processing_error(stage: str, message_id: str, message_type: str) -> str:
        error_id = uuid.uuid4().hex[:8].upper()
        logger.exception(
            "message processing failed error_id=%s stage=%s message_id=%s message_type=%s",
            error_id,
            stage,
            message_id,
            message_type,
        )
        return error_id

    @staticmethod
    def _stage_error_message(stage: str) -> str:
        messages = {
            "message_decode": "消息内容格式无效，请重新发送。",
            "image_download": "图片下载失败，请确认机器人具有读取图片资源的权限后重试。",
            "audio_download": "音频下载失败，请确认机器人具有读取文件资源的权限后重试。",
            "transcription": "语音转写失败，请稍后重试或改用文字。",
            "vision_interpretation": "图片识别服务调用失败，请稍后重试。",
            "interpretation": "指令识别服务调用失败，请稍后重试。",
            "persistence": "账目保存失败，本次未确认入账，请联系管理员检查数据库日志。",
            "pending_create": "创建待确认单失败，请稍后重试。",
            "report_reply": "报告生成或发送失败，请稍后重试。",
            "export_reply": "账单已生成，但发送文件失败，请稍后重试。",
        }
        return messages.get(stage, "处理失败，请稍后重试。")

    async def _execute_with_outbox(
        self,
        *,
        event_id: str | None,
        message_id: str,
        user_open_id: str,
        command: ParsedCommand,
        source_type: str,
        source_message_id: str | None,
    ) -> list[ReplyOutbox]:
        """T2: business action + reply intents in ONE transaction.

        ``LedgerService`` only flushes (``commit_changes=False``); the reply
        intents are built from its result and added to the same session; the
        single commit makes business + outbox atomic. Report advice / PNG are
        generated here too so the outbox row is self-contained and a later
        worker never re-calls AI or re-queries the ledger.
        """
        async with self.session_factory() as session:
            context = await IdentityService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=user_open_id,
            )
            result = await ClientApplicationService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
                exchange_rates=self.exchange_rates,
            ).execute_financial(
                context,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
                commit_changes=False,
            )
            rows = await self._build_outbox_rows(
                event_id=event_id,
                message_id=message_id,
                result=result,
                action=command.action,
            )
            session.add_all(rows)
            if event_id is not None:
                parent = await session.get(ProcessedEvent, event_id)
                if parent is not None:
                    # Durable proof independent of outbox retention. This write
                    # commits atomically with every business result and reply
                    # intent, so future worker/manual replay never relies on a
                    # possibly-cleaned outbox row to avoid duplicate business.
                    parent.business_committed_at = datetime.now(UTC)
            await session.commit()
        return rows

    async def _create_pending_with_preview_outbox(
        self,
        *,
        event_id: str | None,
        message_id: str,
        user_open_id: str,
        command: ParsedCommand,
        source_type: str,
        source_message_id: str | None,
        source_fingerprint: str | None,
        risk: RiskAssessment,
    ) -> list[ReplyOutbox]:
        """Create a pending confirmation + its preview card in ONE transaction.

        The original event is not left ``processing``: the pending row plus the
        preview outbox are committed together with ``business_committed_at``, so
        a crash between this commit and the event status update converges on
        re-claim (the outbox pre-check skips business) without a second pending.
        """
        async with self.session_factory() as session:
            context = await IdentityService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=user_open_id,
            )
            pending = await self._pending_store.create_pending(
                session=session,
                event_id=event_id,
                message_id=message_id,
                source_fingerprint=source_fingerprint,
                user_open_id=user_open_id,
                command=command,
                source_type=source_type,
                risk=risk,
                now=datetime.now(UTC),
                context=context,
            )
            row = self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.CARD,
                sequence=0,
                payload=build_card_payload(
                    card=build_pending_preview_card(
                        PendingPreview.from_json(pending.preview_json),
                        timezone=self.settings.timezone,
                    )
                ),
                blob=None,
            )
            session.add(row)
            if event_id is not None:
                # Keep the fingerprint race inside the explicit commit below;
                # this lookup must not autoflush the pending insert first.
                with session.no_autoflush:
                    parent = await session.get(ProcessedEvent, event_id)
                if parent is not None:
                    parent.business_committed_at = datetime.now(UTC)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                duplicate_exists = (
                    source_fingerprint is not None
                    and await self._pending_store.has_active_fingerprint(
                        context.ledger_id, source_fingerprint
                    )
                )
                if not duplicate_exists:
                    raise
                if event_id is not None:
                    parent = await session.get(ProcessedEvent, event_id)
                    if parent is not None:
                        parent.business_committed_at = datetime.now(UTC)
                        await session.commit()
                logger.info(
                    "concurrent duplicate media pending suppressed event_id=%s message_id=%s",
                    event_id,
                    message_id,
                )
                return []
        logger.info(
            "pending confirmation created confirmation_code=%s event_id=%s risk_reason=%s",
            pending.confirmation_code,
            event_id,
            pending.risk_reason,
        )
        return [row]

    async def _mark_event_business_committed(self, event_id: str | None) -> None:
        if event_id is None:
            return
        async with self.session_factory() as session:
            parent = await session.get(ProcessedEvent, event_id)
            if parent is not None:
                parent.business_committed_at = datetime.now(UTC)
                await session.commit()

    async def _signal_or_deliver(self, outbox_rows: list[ReplyOutbox]) -> None:
        """Deliver committed outbox rows: wake the Reply Worker or send inline."""
        if not outbox_rows:
            return
        if self._reply_worker_enabled:
            # The background Reply Worker owns delivery: signal it and let the
            # DB lease coordinate. A lost wakeup only delays one poll.
            if self._wakeup is not None:
                self._wakeup()
        else:
            await self._sync_deliver_rows(outbox_rows)

    async def _handle_pending_directive(
        self,
        directive: PendingDirective,
        *,
        message_id: str,
        user_open_id: str,
        event_id: str | None,
    ) -> list[ReplyOutbox]:
        """Dispatch a 确认 / 取消 / 查看待确认 directive to the pending store.

        The directive is a new event; the pending store commits its result reply
        (bound to this event) so a re-delivery converges without re-execution.
        """
        now = datetime.now(UTC)
        if directive.action == "confirm":
            assert directive.confirmation_code is not None
            _, rows = await self._pending_store.confirm_and_execute(
                user_open_id=user_open_id,
                confirmation_code=directive.confirmation_code,
                reply_to_message_id=message_id,
                confirm_event_id=event_id,
                exchange_rates=self.exchange_rates,
                now=now,
            )
            return rows
        if directive.action == "cancel":
            assert directive.confirmation_code is not None
            _, rows = await self._pending_store.cancel(
                user_open_id=user_open_id,
                confirmation_code=directive.confirmation_code,
                reply_to_message_id=message_id,
                cancel_event_id=event_id,
                now=now,
            )
            return rows
        _, rows = await self._pending_store.list_pending(
            user_open_id=user_open_id,
            reply_to_message_id=message_id,
            event_id=event_id,
        )
        return rows

    async def _current_ledger_id(self, user_open_id: str) -> uuid.UUID:
        async with self.session_factory() as session:
            context = await IdentityService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
            ).resolve_or_bootstrap(channel="feishu", external_subject_id=user_open_id)
            await session.commit()
            return context.ledger_id

    async def _handle_ledger_command(
        self,
        command: LedgerCommand,
        *,
        message_id: str,
        user_open_id: str,
        event_id: str | None,
    ) -> list[ReplyOutbox]:
        async with self.session_factory() as session:
            context = await IdentityService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
            ).resolve_or_bootstrap(channel="feishu", external_subject_id=user_open_id)
            application = ClientApplicationService(
                session, currency=self.settings.currency, timezone=self.settings.timezone
            )
            try:
                if command.action is LedgerCommandAction.LIST:
                    ledgers = await application.list_personal_ledgers(context)
                    lines = [
                        f"{'→' if item.id == context.ledger_id else ' '} {item.name}"
                        f"{'（默认）' if item.is_default else ''}"
                        for item in ledgers
                    ]
                    reply_text = "账本列表：\n" + "\n".join(lines)
                elif command.action is LedgerCommandAction.CURRENT:
                    ledger = await application.current_personal_ledger(context)
                    reply_text = f"当前账本：{ledger.name}{'（默认）' if ledger.is_default else ''}"
                elif command.action is LedgerCommandAction.CREATE:
                    assert command.name is not None
                    ledger = await application.create_personal_ledger(context, command.name)
                    reply_text = f"已创建账本：{ledger.name}。当前账本仍为原账本。"
                else:
                    assert command.name is not None
                    ledger = await application.find_personal_ledger(context, command.name)
                    if command.action is LedgerCommandAction.SELECT:
                        if context.channel_identity_id is None:
                            raise LedgerManagementError("当前入口身份无法保存账本选择")
                        await application.select_channel_ledger(context, ledger.id)
                        reply_text = f"已切换当前账本：{ledger.name}"
                    elif command.action is LedgerCommandAction.SET_DEFAULT:
                        ledger = await application.set_default_ledger(context, ledger.id)
                        reply_text = f"已将默认账本设为：{ledger.name}。当前账本未切换。"
                    else:
                        assert command.new_name is not None
                        ledger = await application.rename_personal_ledger(
                            context, ledger.id, command.new_name
                        )
                        reply_text = f"账本已重命名为：{ledger.name}"
            except LedgerManagementError as exc:
                reply_text = str(exc)

            row = self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.TEXT,
                sequence=0,
                payload=build_text_payload(reply_text),
                blob=None,
            )
            session.add(row)
            if event_id is not None:
                parent = await session.get(ProcessedEvent, event_id)
                if parent is not None:
                    parent.business_committed_at = datetime.now(UTC)
            await session.commit()
            return [row]

    async def _handle_household_command(
        self,
        command: HouseholdCommand,
        *,
        message_id: str,
        user_open_id: str,
        event_id: str | None,
    ) -> list[ReplyOutbox]:
        async with self.session_factory() as session:
            context = await IdentityService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
            ).resolve_or_bootstrap(channel="feishu", external_subject_id=user_open_id)
            application = ClientApplicationService(
                session, currency=self.settings.currency, timezone=self.settings.timezone
            )
            try:
                if command.action is HouseholdCommandAction.CREATE:
                    assert command.argument is not None
                    view = await application.create_household(context, command.argument)
                    reply_text = (
                        f"已创建家庭：{view.household.name}\n"
                        f"公共账本：{view.ledger.name}\n"
                        "当前账本未切换。"
                    )
                elif command.action is HouseholdCommandAction.LIST:
                    households = await application.list_households(context)
                    if not households:
                        reply_text = "当前尚未加入家庭空间。"
                    else:
                        lines = [
                            f"{item.household.name} · 公共账本：{item.ledger.name} · "
                            f"角色：{item.membership.role}"
                            for item in households
                        ]
                        reply_text = "家庭列表：\n" + "\n".join(lines)
                elif command.action is HouseholdCommandAction.INVITATIONS:
                    invitations = await application.list_household_invitations(context)
                    pending = [item for item in invitations if item.status == "pending"]
                    if not pending:
                        reply_text = "当前没有待处理的家庭邀请。"
                    else:
                        lines = []
                        for invitation in pending:
                            household = await session.get(Household, invitation.household_id)
                            lines.append(
                                f"{invitation.public_id} · "
                                f"{household.name if household else '未知家庭'} · "
                                f"过期：{invitation.expires_at.isoformat()}"
                            )
                        reply_text = "家庭邀请列表：\n" + "\n".join(lines)
                elif command.action in {
                    HouseholdCommandAction.ACCEPT,
                    HouseholdCommandAction.REJECT,
                }:
                    assert command.argument is not None
                    invitation = await application.respond_household_invitation(
                        context,
                        command.argument,
                        "accept" if command.action is HouseholdCommandAction.ACCEPT else "reject",
                    )
                    household = await session.get(Household, invitation.household_id)
                    ledger = await session.scalar(
                        select(Ledger).where(Ledger.household_id == invitation.household_id)
                    )
                    operation = (
                        "接受" if command.action is HouseholdCommandAction.ACCEPT else "拒绝"
                    )
                    reply_text = (
                        f"已{operation}家庭邀请：{household.name if household else '未知家庭'}。"
                        + (f"\n公共账本：{ledger.name}" if ledger is not None else "")
                    )
                else:
                    household_name = (
                        command.argument
                        if command.action
                        in {
                            HouseholdCommandAction.SELECT_LEDGER,
                            HouseholdCommandAction.LEAVE,
                        }
                        else None
                    )
                    view = await application.resolve_household(context, household_name)
                    if command.action is HouseholdCommandAction.CURRENT:
                        reply_text = (
                            f"当前家庭：{view.household.name}\n公共账本：{view.ledger.name}\n"
                            f"角色：{view.membership.role}"
                        )
                    elif command.action is HouseholdCommandAction.MEMBERS:
                        members = await application.list_household_members(
                            context, view.household.id
                        )
                        lines = [
                            f"{item.user.display_name or str(item.user.id)} · "
                            f"{item.membership.role}"
                            for item in members
                        ]
                        reply_text = (
                            f"家庭：{view.household.name}\n公共账本：{view.ledger.name}\n成员：\n"
                            + "\n".join(lines)
                        )
                    elif command.action is HouseholdCommandAction.INVITE:
                        assert command.argument is not None
                        invitation = await application.invite_household_member(
                            context, view.household.id, command.argument
                        )
                        target = await session.get(User, invitation.target_user_id)
                        target_name = (
                            target.display_name
                            if target and target.display_name
                            else command.argument
                        )
                        reply_text = (
                            f"已邀请 {target_name} 加入家庭："
                            f"{view.household.name}\n公共账本：{view.ledger.name}\n"
                            f"邀请编号：{invitation.public_id}"
                        )
                    elif command.action is HouseholdCommandAction.SELECT_LEDGER:
                        if context.channel_identity_id is None:
                            raise HouseholdManagementError("当前入口身份无法保存账本选择")
                        await application.select_channel_ledger(context, view.ledger.id)
                        reply_text = (
                            f"已切换家庭账本：{view.household.name}\n公共账本：{view.ledger.name}"
                        )
                    else:
                        await application.leave_household(context, view.household.id)
                        reply_text = (
                            f"已退出家庭：{view.household.name}。\n"
                            f"已失去公共账本“{view.ledger.name}”的访问权限，当前账本已回退。"
                        )
            except HouseholdManagementError as exc:
                reply_text = str(exc)

            row = self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.TEXT,
                sequence=0,
                payload=build_text_payload(reply_text),
                blob=None,
            )
            session.add(row)
            if event_id is not None:
                parent = await session.get(ProcessedEvent, event_id)
                if parent is not None:
                    parent.business_committed_at = datetime.now(UTC)
            await session.commit()
            return [row]

    async def _handle_recurring_command(
        self,
        command: RecurringCommand,
        *,
        message_id: str,
        user_open_id: str,
        event_id: str | None,
        now: datetime,
    ) -> list[ReplyOutbox]:
        """Dispatch a deterministic recurring-rule command (P29).

        Commits the rule change and its reply in one transaction so a crash
        never leaves a rule created without its confirmation reply.
        """
        async with self.session_factory() as session:
            context = await IdentityService(
                session,
                currency=self.settings.currency,
                timezone=self.settings.timezone,
            ).resolve_or_bootstrap(channel="feishu", external_subject_id=user_open_id)
            application = ClientApplicationService(
                session, currency=self.settings.currency, timezone=self.settings.timezone
            )
            try:
                if command.action is RecurringCommandAction.CREATE:
                    assert command.frequency is not None
                    assert command.next_occurrence is not None
                    assert command.amount is not None
                    rule = await application.create_recurring_rule(
                        context,
                        transaction_type=(
                            command.transaction_type
                            if command.transaction_type is not None
                            else Direction.EXPENSE
                        ),
                        amount=command.amount,
                        currency=command.currency,
                        category=command.category or "",
                        description=command.description or "",
                        frequency=command.frequency.value,
                        interval=1,
                        next_occurrence=command.next_occurrence,
                        account_id=(await AccountService(session).get_default(context)).id,
                    )
                    reply_text = self._recurring_created_message(rule)
                elif command.action is RecurringCommandAction.LIST:
                    rules = await application.list_recurring_rules(context)
                    reply_text = await self._recurring_list_message(session, context, rules)
                else:
                    rule = await self._resolve_recurring_rule(
                        session, context, command.name
                    )
                    if command.action is RecurringCommandAction.PAUSE:
                        rule = await application.pause_recurring_rule(context, rule.id)
                        reply_text = (
                            f"已暂停周期账单：{rule.description or rule.category}\n"
                            "暂停期间不会产生新的提醒。恢复请发送：恢复"
                            f"{rule.description or rule.category}"
                        )
                    elif command.action is RecurringCommandAction.RESUME:
                        rule = await application.resume_recurring_rule(context, rule.id)
                        reply_text = (
                            f"已恢复周期账单：{rule.description or rule.category}\n"
                            f"下次发生：{rule.next_occurrence.isoformat()}\n"
                            "不会补生成暂停期间的提醒。"
                        )
                    else:
                        rule = await application.skip_recurring_occurrence(context, rule.id)
                        reply_text = (
                            f"已跳过本期：{rule.description or rule.category}\n"
                            f"下次发生：{rule.next_occurrence.isoformat()}\n"
                            "本期不会生成账目，后续周期继续有效。"
                        )
            except RecurringRuleError as exc:
                reply_text = str(exc)

            row = self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.TEXT,
                sequence=0,
                payload=build_text_payload(reply_text),
                blob=None,
            )
            session.add(row)
            if event_id is not None:
                parent = await session.get(ProcessedEvent, event_id)
                if parent is not None:
                    parent.business_committed_at = datetime.now(UTC)
            await session.commit()
            return [row]

    def _recurring_created_message(self, rule: RecurringRule) -> str:
        sign = "收入" if rule.transaction_type is Direction.INCOME else "支出"
        frequency = {
            "weekly": "每周",
            "monthly": "每月",
            "yearly": "每年",
        }.get(rule.frequency, rule.frequency)
        return (
            f"已创建周期账单：\n"
            f"{rule.description or rule.category} · {sign} {self._format_money(rule.amount)}"
            f" · {rule.category}\n"
            f"周期：{frequency} · 下次发生：{rule.next_occurrence.isoformat()}\n"
            "到期后我会先提醒你确认，确认后才正式入账。"
        )

    async def _recurring_list_message(
        self,
        session: AsyncSession,
        context: RequestContext,
        rules: list[RecurringRule],
    ) -> str:
        if not rules:
            return "当前还没有周期账单。可以这样创建：每月8号房租3500"
        names = await self._recurring_account_names(session, context, rules)
        lines = [f"周期账单（{len(rules)}）："]
        for rule in rules:
            lines.append(self._recurring_rule_line(rule, names.get(rule.account_id, "")))
        lines.append("")
        lines.append("暂停/恢复/跳过：暂停房租 / 恢复房租 / 跳过房租")
        return "\n".join(lines)

    def _recurring_rule_line(self, rule: RecurringRule, account_name: str) -> str:
        sign = "收入" if rule.transaction_type is Direction.INCOME else "支出"
        frequency = {
            "weekly": "每周",
            "monthly": "每月",
            "yearly": "每年",
        }.get(rule.frequency, rule.frequency)
        status = {
            "active": "启用",
            "paused": "已暂停",
            "disabled": "已停用",
        }.get(rule.status, rule.status)
        account_part = f" · {account_name}" if account_name else ""
        return (
            f"• {rule.description or rule.category} · {sign} "
            f"{self._format_money(rule.amount)}{account_part}\n"
            f"  {frequency} · 下次 {rule.next_occurrence.isoformat()} · {status}"
        )

    async def _recurring_account_names(
        self,
        session: AsyncSession,
        context: RequestContext,
        rules: list[RecurringRule],
    ) -> dict[uuid.UUID, str]:
        from lark_ledger.models import Account

        account_ids = {rule.account_id for rule in rules if rule.account_id is not None}
        names: dict[uuid.UUID, str] = {}
        if not account_ids:
            return names
        rows = (
            await session.execute(
                select(Account.id, Account.name).where(
                    Account.ledger_id == context.ledger_id,
                    Account.id.in_(account_ids),
                )
            )
        ).all()
        for account_id, name in rows:
            names[account_id] = name
        return names

    async def _resolve_recurring_rule(
        self,
        session: AsyncSession,
        context: RequestContext,
        name: str | None,
    ) -> RecurringRule:
        """Resolve a lifecycle command's target rule by name.

        ``name`` is ``None`` for "跳过本期": the most-due active rule is used.
        """
        from lark_ledger.models import RecurringRuleStatus

        if name is None:
            rule = await session.scalar(
                select(RecurringRule)
                .where(
                    RecurringRule.ledger_id == context.ledger_id,
                    RecurringRule.status == RecurringRuleStatus.ACTIVE.value,
                )
                .order_by(RecurringRule.next_occurrence, RecurringRule.created_at)
                .limit(1)
            )
            if rule is None:
                raise RecurringRuleNotFoundError("当前没有启用中的周期账单")
            return rule
        rows = (
            (
                await session.scalars(
                    select(RecurringRule).where(
                        RecurringRule.ledger_id == context.ledger_id,
                        or_(
                            RecurringRule.description.contains(name),
                            RecurringRule.category.contains(name),
                        ),
                    )
                    .order_by(RecurringRule.created_at.desc(), RecurringRule.id.desc())
                )
            )
            .all()
        )
        if not rows:
            raise RecurringRuleNotFoundError(f"没有找到名称包含“{name}”的周期账单")
        if len(rows) > 1:
            raise RecurringRuleConflictError(
                f"有多个周期账单名称包含“{name}”，请使用更具体的名称"
            )
        return rows[0]

    def _format_money(self, amount: Decimal) -> str:
        if self.settings.currency == "CNY":
            return f"¥{amount:.2f}"
        return f"{amount:.2f} {self.settings.currency}"

    async def _business_result_committed(self, event_id: str) -> bool:
        async with self.session_factory() as session:
            committed_at = await session.scalar(
                select(ProcessedEvent.business_committed_at).where(
                    ProcessedEvent.event_id == event_id
                )
            )
            return committed_at is not None

    async def _build_outbox_rows(
        self,
        *,
        event_id: str | None,
        message_id: str,
        result: ExecutionResult,
        action: Action,
    ) -> list[ReplyOutbox]:
        if action is Action.REPORT:
            return await self._build_report_outbox_rows(event_id, message_id, result)
        if action is Action.EXPORT_ENTRIES:
            return self._build_export_outbox_rows(event_id, message_id, result)
        message_text = result.message
        if result.budget_alert:
            message_text = f"{message_text}\n\n{result.budget_alert}"
        return [
            self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.TEXT,
                sequence=0,
                payload=build_text_payload(message_text),
                blob=None,
            )
        ]

    def _build_export_outbox_rows(
        self,
        event_id: str | None,
        message_id: str,
        result: ExecutionResult,
    ) -> list[ReplyOutbox]:
        export = result.export
        if export is None:
            return [
                self._make_outbox_row(
                    event_id=event_id,
                    message_id=message_id,
                    reply_type=ReplyType.TEXT,
                    sequence=0,
                    payload=build_text_payload(result.message),
                    blob=None,
                )
            ]
        return [
            self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.FILE,
                sequence=0,
                payload=build_file_payload(
                    filename=export.filename,
                    content_type="text/csv",
                    content=export.content,
                ),
                blob=export.content,
            ),
            self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.TEXT,
                sequence=1,
                payload=build_text_payload(result.message),
                blob=None,
            ),
        ]

    async def _build_report_outbox_rows(
        self,
        event_id: str | None,
        message_id: str,
        result: ExecutionResult,
    ) -> list[ReplyOutbox]:
        report = result.report
        if report is None:
            return [
                self._make_outbox_row(
                    event_id=event_id,
                    message_id=message_id,
                    reply_type=ReplyType.CARD,
                    sequence=0,
                    payload=build_card_payload(card=build_report_card(None, result.message)),
                    blob=None,
                )
            ]
        try:
            advice = await self.interpreter.generate_advice(report)
        except Exception:
            logger.exception("AI advice generation failed; using deterministic fallback")
            advice = fallback_advice(report)
        png: bytes | None = None
        try:
            png = self.renderer.render(report, advice)
        except Exception:
            logger.exception("report rendering failed; sending text-only card")
        image_alt: str | None = None
        if png is not None:
            range_text = (
                f"{report.range_start.date().isoformat()} 至 "
                f"{(report.range_end - timedelta(microseconds=1)).date().isoformat()}"
            )
            image_alt = f"{range_text}消费报告图表，{result.message}"
        card = build_report_card(report, result.message, advice=advice, image_key=None)
        return [
            self._make_outbox_row(
                event_id=event_id,
                message_id=message_id,
                reply_type=ReplyType.CARD,
                sequence=0,
                payload=build_card_payload(
                    card=card,
                    image_bytes=png,
                    image_alt=image_alt,
                ),
                blob=png,
            )
        ]

    @staticmethod
    def _make_outbox_row(
        *,
        event_id: str | None,
        message_id: str,
        reply_type: ReplyType,
        sequence: int,
        payload: dict[str, Any],
        blob: bytes | None,
    ) -> ReplyOutbox:
        return ReplyOutbox(
            event_id=event_id,
            message_id=message_id,
            reply_type=reply_type.value,
            sequence=sequence,
            transport="feishu",
            payload_version=OUTBOX_PAYLOAD_VERSION,
            payload_json=payload,
            payload_blob=blob,
            status=ReplyStatus.PENDING.value,
            attempt_count=0,
        )

    async def _sync_deliver_rows(self, rows: list[ReplyOutbox]) -> None:
        """T3 compatible send: claim + deliver the committed intents inline.

        Runs only when the background Reply Worker is disabled. Every row is
        claimed with the same lease-guarded primitives the worker uses
        (``claim_by_id`` → ``ReplyDeliverer`` → outcome update), so the
        synchronous path never bypasses the outbox state guards. Rows are
        delivered in sequence order; an earlier row that is neither ``sent``
        nor ``dead`` stops later rows (file before its confirmation text), and
        a failed file delivery keeps the v0.2.0 direct fallback notice. A
        delivery failure marks the row ``failed`` / ``dead`` and never re-runs
        business and never fails the event.
        """
        if not rows:
            return
        now = datetime.now(UTC)
        ordered = sorted(rows, key=lambda row: row.sequence)
        for row in ordered:
            item = await self.outbox_store.claim_by_id(
                row.id, self._sync_owner, now, lease_seconds=self.settings.reply_lease_seconds
            )
            if item is None:
                return
            outcome = await self._reply_deliverer.process_item(item, now)
            if outcome == ReplyStatus.FAILED.value and item.reply_type == ReplyType.FILE.value:
                await self._notify_export_send_failed(item.message_id)
            if outcome != ReplyStatus.SENT.value:
                # failed / dead / lease-lost: an earlier reply that is not
                # ``sent`` stops later rows, matching the worker's ordering rule.
                return

    async def _notify_export_send_failed(self, message_id: str) -> None:
        """Best-effort direct notice outside the outbox (v0.2.0 UX)."""
        try:
            await self.feishu.reply_text(message_id, "账单已生成，但发送文件失败，请稍后重试。")
        except Exception:
            logger.exception("failed to send export failure notice message_id=%s", message_id)
