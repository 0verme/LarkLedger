import asyncio
import base64
import hashlib
import hmac
import json
import logging
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.entry_commands import (
    bind_entry_refs_from_message,
    try_parse_deterministic_entry_command,
)
from lark_ledger.models import ReplyOutbox
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_card_payload,
    build_file_payload,
    build_text_payload,
)
from lark_ledger.schemas import (
    MAX_BATCH_BUDGETS,
    MAX_BATCH_ENTRIES,
    MAX_EXPORT_BYTES,
    Action,
    ExecutionResult,
    ParsedCommand,
)
from lark_ledger.services.ai import AIInterpreter, CommandInterpretationError
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.outbox import ReplyOutboxStore, record_failure_summary
from lark_ledger.services.report import ReportRenderer, build_report_card, fallback_advice

logger = logging.getLogger(__name__)

MAX_POST_IMAGES = 5


def _safe_export_filename(filename: str) -> str:
    """Accept only a basenamed application export file (no path segments)."""
    if not filename or filename != Path(filename).name:
        raise ValueError("导出文件名无效")
    safe_name = Path(filename).name
    if safe_name in {".", ".."} or ".." in safe_name:
        raise ValueError("导出文件名无效")
    if not safe_name.endswith(".csv"):
        raise ValueError("导出文件名无效")
    return safe_name


def _write_export_temp_file(content: bytes, safe_name: str) -> Path:
    """Write export bytes under the system temp dir; caller must delete the path."""
    suffix = Path(safe_name).suffix or ".csv"
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="larkledger-export-",
        suffix=suffix,
        delete=False,
    )
    try:
        handle.write(content)
        handle.flush()
        return Path(handle.name)
    finally:
        handle.close()


class FeishuClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            response = await self._client.request(method, path, **kwargs)
        else:
            async with httpx.AsyncClient(
                base_url=self.settings.lark_base_url.rstrip("/"), timeout=30
            ) as client:
                response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def tenant_token(self) -> str:
        loop = asyncio.get_running_loop()
        if self._token and loop.time() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and loop.time() < self._token_expires_at:
                return self._token
            response = await self._request(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.settings.lark_app_id,
                    "app_secret": self.settings.lark_app_secret,
                },
            )
            payload = response.json()
            if payload.get("code") != 0:
                message = payload.get("msg", "unknown")
                raise RuntimeError(f"获取 tenant_access_token 失败：{message}")
            self._token = str(payload["tenant_access_token"])
            self._token_expires_at = loop.time() + int(payload.get("expire", 7200)) - 60
            return self._token

    async def reply_text(self, message_id: str, text: str) -> None:
        await self._reply_message(message_id, "text", {"text": text})

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> None:
        await self._reply_message(message_id, "interactive", card)

    async def _reply_message(
        self, message_id: str, message_type: str, content: dict[str, Any]
    ) -> None:
        token = await self.tenant_token()
        response = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": message_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"回复飞书消息失败：{payload.get('msg', 'unknown')}")

    async def upload_image(self, png: bytes) -> str:
        if not png:
            raise ValueError("报告图片不能为空")
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("报告图片必须是 PNG 格式")
        if len(png) > 10 * 1024 * 1024:
            raise ValueError("报告图片不能超过 10 MB")
        token = await self.tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("consumption-report.png", png, "image/png")},
        )
        payload = response.json()
        image_key = payload.get("data", {}).get("image_key")
        if payload.get("code") != 0 or not isinstance(image_key, str) or not image_key:
            raise RuntimeError(f"上传飞书图片失败：{payload.get('msg', 'unknown')}")
        return image_key

    async def upload_file(self, content: bytes, filename: str) -> str:
        """Upload a message file and return Feishu ``file_key``.

        Content is written to a secure temporary file for the multipart request,
        then deleted in ``finally`` on both success and failure. The filename is
        application-generated; user input never becomes a path.
        """
        if not content:
            raise ValueError("导出文件不能为空")
        if len(content) > MAX_EXPORT_BYTES:
            raise ValueError("导出文件不能超过 5 MB")
        safe_name = _safe_export_filename(filename)
        token = await self.tenant_token()
        temp_path: Path | None = None
        try:
            temp_path = _write_export_temp_file(content, safe_name)
            with temp_path.open("rb") as handle:
                response = await self._request(
                    "POST",
                    "/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "stream", "file_name": safe_name},
                    files={"file": (safe_name, handle, "text/csv")},
                )
            payload = response.json()
            file_key = payload.get("data", {}).get("file_key")
            if payload.get("code") != 0 or not isinstance(file_key, str) or not file_key:
                raise RuntimeError(f"上传飞书文件失败：{payload.get('msg', 'unknown')}")
            return file_key
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "failed to remove export temp file after upload name=%s",
                        safe_name,
                    )

    async def reply_file(self, message_id: str, file_key: str) -> None:
        await self._reply_message(message_id, "file", {"file_key": file_key})

    async def download_resource(self, message_id: str, file_key: str, kind: str) -> bytes:
        token = await self.tenant_token()
        response = await self._request(
            "GET",
            f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {token}"},
            params={"type": "image" if kind == "image" else "file"},
        )
        return response.content


def verify_signature(raw_body: bytes, timestamp: str, nonce: str, signature: str, key: str) -> bool:
    material = timestamp.encode() + nonce.encode() + key.encode() + raw_body
    expected = hashlib.sha256(material).hexdigest()
    return hmac.compare_digest(expected, signature)


def decrypt_event(encrypted: str, key: str) -> dict[str, Any]:
    digest = hashlib.sha256(key.encode()).digest()
    raw = base64.b64decode(encrypted)
    if len(raw) < 32 or len(raw) % 16:
        raise ValueError("invalid encrypted event")
    decryptor = Cipher(algorithms.AES(digest), modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    result = json.loads(plain)
    if not isinstance(result, dict):
        raise ValueError("decrypted event is not an object")
    return result


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
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.feishu = feishu
        self.interpreter = interpreter
        self.renderer = renderer or ReportRenderer(settings.report_font_path)
        self.exchange_rates = exchange_rates or ExchangeRateService(settings)
        self.outbox_store = outbox_store or ReplyOutboxStore(session_factory)

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
        stage = "message_decode"
        is_visual_message = message_type == "image"

        # Crash-window recovery (P06a): a ``reply_outbox`` row exists only if the
        # business transaction it was written with committed. When an event is
        # re-delivered after a crash between that commit and the event status
        # update, skip business entirely (no duplicate entries / outbox rows)
        # and let the worker converge the event to ``succeeded``.
        if event_id is not None and await self.outbox_store.has_outbox(event_id):
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
                    await self.feishu.reply_text(
                        message_id, "这条富文本中没有可识别的文字或图片。"
                    )
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
            if not images:
                deterministic = try_parse_deterministic_entry_command(text)
                if isinstance(deterministic, str):
                    await self.feishu.reply_text(message_id, deterministic)
                    return
                if deterministic is not None:
                    command = deterministic
            if command is None:
                command = await self.interpreter.interpret(text, now=now, images=images)
                bound = bind_entry_refs_from_message(command, text)
                if isinstance(bound, str):
                    await self.feishu.reply_text(message_id, bound)
                    return
                command = bound
            stage = "persistence"
            outbox_rows = await self._execute_with_outbox(
                event_id=event_id,
                message_id=message_id,
                user_open_id=user_open_id,
                command=command,
                source_type=source_type,
                source_message_id=(
                    message_id
                    if command.action in {Action.CREATE, Action.CREATE_ENTRIES, Action.BATCH}
                    else None
                ),
            )
            # From here the business + outbox are already committed; a post-commit
            # bookkeeping failure must not send a misleading "save failed" reply.
            stage = "reply"
            await self._send_outbox_rows(outbox_rows)
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
            result = await LedgerService(
                session,
                self.settings.currency,
                self.settings.timezone,
                exchange_rates=self.exchange_rates,
                commit_changes=False,
            ).execute(
                user_open_id,
                command,
                source_type=source_type,
                source_message_id=source_message_id,
            )
            rows = await self._build_outbox_rows(
                event_id=event_id,
                message_id=message_id,
                result=result,
                action=command.action,
            )
            session.add_all(rows)
            await session.commit()
        return rows

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

    async def _send_outbox_rows(self, rows: list[ReplyOutbox]) -> None:
        """T3: one compatible send of the committed intents (never a worker).

        Rows are delivered in sequence order. A delivery failure marks the row
        ``failed`` for a P06b worker and never re-runs business; the event is
        not affected. A failed file delivery keeps the v0.2.0 UX of a direct
        fallback notice and stops later rows (file before its confirmation
        text).
        """
        if not rows:
            return
        committed = await self.outbox_store.load_by_ids([row.id for row in rows])
        committed.sort(key=lambda row: row.sequence)
        for row in committed:
            try:
                await self._deliver(row)
            except Exception as exc:
                error_code, summary = record_failure_summary(exc)
                await self.outbox_store.mark_failed(
                    row.id, error_code=error_code, summary=summary
                )
                logger.warning(
                    "outbox compatible send failed outbox_id=%s event_id=%s "
                    "reply_type=%s error_code=%s",
                    row.id,
                    row.event_id,
                    row.reply_type,
                    error_code,
                )
                if row.reply_type == ReplyType.FILE.value:
                    await self._notify_export_send_failed(row.message_id)
                return
            else:
                await self.outbox_store.mark_sent(row.id, result_summary="delivered")

    async def _notify_export_send_failed(self, message_id: str) -> None:
        """Best-effort direct notice outside the outbox (v0.2.0 UX)."""
        try:
            await self.feishu.reply_text(
                message_id, "账单已生成，但发送文件失败，请稍后重试。"
            )
        except Exception:
            logger.exception(
                "failed to send export failure notice message_id=%s", message_id
            )

    async def _deliver(self, row: ReplyOutbox) -> None:
        """Deliver one committed outbox row via Feishu.

        Consumes only data persisted on the row (no AI, no ledger re-query, no
        temporary file): text is sent verbatim, file bytes are uploaded from
        ``payload_blob``, and a card either carries its pre-rendered PNG or is
        sent text-only. A card whose image upload fails degrades to the
        text-only card already stored on the row.
        """
        payload = row.payload_json
        if row.reply_type == ReplyType.TEXT.value:
            await self.feishu.reply_text(row.message_id, payload["text"])
            return
        if row.reply_type == ReplyType.FILE.value:
            if row.payload_blob is None:
                raise ValueError("file outbox row is missing payload_blob")
            file_meta = payload["file"]
            file_key = await self.feishu.upload_file(
                row.payload_blob, file_meta["filename"]
            )
            await self.feishu.reply_file(row.message_id, file_key)
            return
        if row.reply_type == ReplyType.CARD.value:
            card = dict(payload["card"])
            if row.payload_blob is not None:
                try:
                    image_key = await self.feishu.upload_image(row.payload_blob)
                except Exception:
                    logger.exception(
                        "report image upload failed; sending text-only card "
                        "message_id=%s",
                        row.message_id,
                    )
                    image_key = None
                if image_key:
                    image_meta = payload.get("image") or {}
                    card = self._card_with_image(
                        card, image_key, str(image_meta.get("alt") or "")
                    )
            await self.feishu.reply_card(row.message_id, card)
            return
        raise RuntimeError(f"unsupported reply_type: {row.reply_type}")

    @staticmethod
    def _card_with_image(
        card: dict[str, Any], image_key: str, alt: str
    ) -> dict[str, Any]:
        """Return the stored card with its pre-rendered image element injected.

        The stored card body is the text-only variant (message + advice); at
        send time the advice element is replaced by the uploaded image so the
        delivered card carries exactly the chart the sender rendered.
        """
        elements = card.get("body", {}).get("elements", [])
        message_element = elements[0] if elements else {"tag": "markdown", "content": ""}
        body = dict(card.get("body") or {})
        body["elements"] = [
            message_element,
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": alt},
                "scale_type": "fit_horizontal",
                "preview": True,
            },
        ]
        rebuilt = dict(card)
        rebuilt["body"] = body
        return rebuilt
