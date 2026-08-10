"""Feishu HTTP client and media/export helpers (split from ``feishu.py``).

``lark_ledger.services.feishu`` re-exports these names unchanged.
"""

import asyncio
import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx

from lark_ledger.config import Settings
from lark_ledger.event_payload import safe_error_summary
from lark_ledger.schemas import MAX_EXPORT_BYTES

logger = logging.getLogger("lark_ledger.services.feishu")

def _media_fingerprint(source_type: str, text: str, images: list[bytes]) -> str:
    """Hash an ordered visual payload without retaining its private contents."""
    digest = hashlib.sha256()
    digest.update(b"lark-ledger:visual-fingerprint:v1\0")
    digest.update(source_type.encode("utf-8"))
    digest.update(b"\0")
    encoded_text = text.encode("utf-8")
    digest.update(len(encoded_text).to_bytes(8, "big"))
    digest.update(encoded_text)
    for image in images:
        digest.update(len(image).to_bytes(8, "big"))
        digest.update(image)
    return digest.hexdigest()

def _feishu_error_details(response: httpx.Response) -> tuple[str, str]:
    """Return bounded, secret-redacted Feishu error fields without response content."""
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return "-", "-"
    if not isinstance(payload, dict):
        return "-", "-"

    raw_code = payload.get("code")
    api_code = str(raw_code)[:64] if isinstance(raw_code, (str, int)) else "-"
    raw_message = payload.get("msg") or payload.get("message")
    if not isinstance(raw_message, str) or not raw_message.strip():
        return api_code, "-"
    summary = safe_error_summary(RuntimeError(raw_message), max_length=256)
    return api_code, summary.removeprefix("RuntimeError: ")

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
        if not response.is_success:
            api_code, detail = _feishu_error_details(response)
            logger.warning(
                "Feishu API request rejected method=%s status=%d api_code=%s detail=%s",
                method.upper(),
                response.status_code,
                api_code,
                detail,
            )
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

    async def reply_text(
        self, message_id: str, text: str, *, uuid: str | None = None
    ) -> str | None:
        return await self._reply_message(message_id, "text", {"text": text}, uuid=uuid)

    async def reply_card(
        self, message_id: str, card: dict[str, Any], *, uuid: str | None = None
    ) -> str | None:
        return await self._reply_message(message_id, "interactive", card, uuid=uuid)

    async def reply_file(
        self, message_id: str, file_key: str, *, uuid: str | None = None
    ) -> str | None:
        return await self._reply_message(
            message_id, "file", {"file_key": file_key}, uuid=uuid
        )

    async def _reply_message(
        self,
        message_id: str,
        message_type: str,
        content: dict[str, Any],
        *,
        uuid: str | None = None,
    ) -> str | None:
        """Reply to a message and return the remote reply ``message_id``.

        The reply API supports a client-supplied ``uuid`` idempotency key
        (≤50 chars): within one hour the same uuid is delivered at most once and
        a repeat call returns the already-created ``message_id``. The Reply
        Worker passes the outbox row id (32-hex) so a retry after a "sent but
        not marked" crash is deduplicated instead of reaching the user twice.
        """
        body: dict[str, Any] = {
            "msg_type": message_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if uuid:
            body["uuid"] = uuid
        token = await self.tenant_token()
        response = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"回复飞书消息失败：{payload.get('msg', 'unknown')}")
        remote_id = (payload.get("data") or {}).get("message_id")
        return str(remote_id) if isinstance(remote_id, str) and remote_id else None

    async def _send_message(
        self,
        open_id: str,
        message_type: str,
        content: dict[str, Any],
        *,
        uuid: str | None = None,
    ) -> str | None:
        """Send a proactive message to a user (not a reply), returning its id.

        Used for P29 due-bill reminders when there is no message to reply to.
        The same ``uuid`` idempotency key semantics apply as ``_reply_message``.
        """
        body: dict[str, Any] = {
            "receive_id_type": "open_id",
            "receive_id": open_id,
            "msg_type": message_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if uuid:
            body["uuid"] = uuid
        token = await self.tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"发送飞书消息失败：{payload.get('msg', 'unknown')}")
        remote_id = (payload.get("data") or {}).get("message_id")
        return str(remote_id) if isinstance(remote_id, str) and remote_id else None

    async def send_text(
        self, open_id: str, text: str, *, uuid: str | None = None
    ) -> str | None:
        return await self._send_message(open_id, "text", {"text": text}, uuid=uuid)

    async def send_card(
        self, open_id: str, card: dict[str, Any], *, uuid: str | None = None
    ) -> str | None:
        return await self._send_message(open_id, "interactive", card, uuid=uuid)

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
        from lark_ledger.services import feishu as _facade
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
            temp_path = _facade._write_export_temp_file(content, safe_name)
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

    async def download_resource(self, message_id: str, file_key: str, kind: str) -> bytes:
        token = await self.tenant_token()
        response = await self._request(
            "GET",
            f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {token}"},
            params={"type": "image" if kind == "image" else "file"},
        )
        return response.content
