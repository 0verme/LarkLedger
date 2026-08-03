import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.schemas import Action, ExecutionResult, ParsedCommand
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.report import ReportRenderer, build_report_card, fallback_advice

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.feishu = feishu
        self.interpreter = interpreter
        self.renderer = renderer or ReportRenderer(settings.report_font_path)
        self.exchange_rates = exchange_rates or ExchangeRateService(settings)

    async def process(self, event: dict[str, Any]) -> None:
        message = event["message"]
        message_id = str(message["message_id"])
        sender = event.get("sender", {}).get("sender_id", {})
        user_open_id = str(sender.get("open_id") or sender.get("user_id") or "")
        if not user_open_id:
            return
        message_type = str(message.get("message_type", ""))
        command: ParsedCommand | None = None
        try:
            content = json.loads(message.get("content", "{}"))
            now = datetime.now(ZoneInfo(self.settings.timezone))
            image: bytes | None = None
            text = ""
            source_type = message_type
            if message_type == "text":
                text = str(content.get("text", "")).strip()
            elif message_type == "image":
                if not self.interpreter.vision_configured:
                    await self.feishu.reply_text(message_id, "图片识别功能尚未配置。")
                    return
                image = await self.feishu.download_resource(
                    message_id, str(content["image_key"]), "image"
                )
                text = "识别这张小票或支付截图并记账"
            elif message_type in {"audio", "file"}:
                if not self.interpreter.transcription_configured:
                    await self.feishu.reply_text(message_id, "语音识别功能尚未配置。")
                    return
                file_key = str(content.get("file_key", ""))
                if not file_key:
                    raise ValueError("消息中没有 file_key")
                audio = await self.feishu.download_resource(message_id, file_key, "file")
                text = await self.interpreter.transcribe(
                    audio, filename=str(content.get("file_name", "voice.opus"))
                )
                source_type = "audio"
            else:
                await self.feishu.reply_text(
                    message_id, "暂时只支持文字、语音、小票照片和支付截图。"
                )
                return
            command = await self.interpreter.interpret(text, now=now, image=image)
            async with self.session_factory() as session:
                result = await LedgerService(
                    session,
                    self.settings.currency,
                    self.settings.timezone,
                    exchange_rates=self.exchange_rates,
                ).execute(
                    user_open_id,
                    command,
                    source_type=source_type,
                    source_message_id=message_id if command.action.value == "create" else None,
                )
            if command.action is Action.REPORT:
                await self._reply_report(message_id, result)
            else:
                message_text = result.message
                if result.budget_alert:
                    message_text = f"{message_text}\n\n{result.budget_alert}"
                await self.feishu.reply_text(message_id, message_text)
        except ExchangeRateUnavailableError:
            await self.feishu.reply_text(message_id, "暂时无法获取汇率，请稍后重试。")
        except Exception:
            if command is None or command.action is not Action.REPORT:
                await self.feishu.reply_text(message_id, "处理失败了，请稍后重试或换一种说法。")
            raise

    async def _reply_report(self, message_id: str, result: ExecutionResult) -> None:
        if result.report is None:
            await self.feishu.reply_card(
                message_id,
                build_report_card(None, result.message),
            )
            return

        report = result.report
        try:
            advice = await self.interpreter.generate_advice(report)
        except Exception:
            logger.exception("AI advice generation failed; using deterministic fallback")
            advice = fallback_advice(report)

        image_key: str | None = None
        try:
            png = self.renderer.render(report, advice)
            image_key = await self.feishu.upload_image(png)
        except Exception:
            logger.exception("report rendering or upload failed; sending text-only card")

        card = build_report_card(
            report,
            result.message,
            advice=advice,
            image_key=image_key,
        )
        await self.feishu.reply_card(message_id, card)
