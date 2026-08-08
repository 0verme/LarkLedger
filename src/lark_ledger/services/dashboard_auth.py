"""Feishu OAuth and revocable PostgreSQL sessions for the Web Dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.context import RequestContext
from lark_ledger.models import DashboardSession
from lark_ledger.services.identity import IdentityService

SESSION_COOKIE = "lark_ledger_session"
CSRF_COOKIE = "lark_ledger_csrf"
OAUTH_COOKIE = "lark_ledger_oauth"
CSRF_HEADER = "X-CSRF-Token"


class DashboardAuthError(ValueError):
    """Safe authentication failure suitable for a localized API response."""


@dataclass(frozen=True)
class DashboardPrincipal:
    session_id: str
    user_id: uuid.UUID
    ledger_id: uuid.UUID
    user_open_id: str
    display_name: str
    avatar_url: str
    role: str
    expires_at: datetime

    @property
    def request_context(self) -> RequestContext:
        return RequestContext(
            actor_user_id=self.user_id,
            ledger_id=self.ledger_id,
            source_channel="web",
            external_subject_id=self.user_open_id,
        )


@dataclass(frozen=True)
class CreatedSession:
    principal: DashboardPrincipal
    session_token: str
    csrf_token: str


@dataclass(frozen=True)
class OAuthRequest:
    authorize_url: str
    state_cookie: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_next_path(value: str | None) -> str:
    candidate = (value or "/").strip()
    parsed = urlsplit(candidate)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or parsed.scheme
        or parsed.netloc
    ):
        raise DashboardAuthError("登录后跳转地址无效")
    return candidate


class DashboardAuthService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._factory = session_factory
        self._client = client
        key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.dashboard_session_secret.encode("utf-8")).digest()
        )
        self._fernet = Fernet(key)

    @property
    def callback_url(self) -> str:
        return self._settings.dashboard_base_url.rstrip("/") + "/api/web/v1/auth/callback"

    def begin_oauth(self, next_path: str | None) -> OAuthRequest:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        envelope = self._fernet.encrypt(
            json.dumps(
                {
                    "state": state,
                    "verifier": verifier,
                    "next": safe_next_path(next_path),
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        query = urlencode(
            {
                "client_id": self._settings.lark_app_id,
                "response_type": "code",
                "redirect_uri": self.callback_url,
                "scope": "auth:user.id:read",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return OAuthRequest(
            authorize_url=f"{self._settings.dashboard_oauth_authorize_url}?{query}",
            state_cookie=envelope,
        )

    def complete_oauth_state(self, cookie: str | None, state: str) -> tuple[str, str]:
        if not cookie or not state:
            raise DashboardAuthError("OAuth state 缺失")
        try:
            raw = self._fernet.decrypt(
                cookie.encode("ascii"),
                ttl=self._settings.dashboard_oauth_state_ttl_seconds,
            )
            payload = json.loads(raw)
        except (InvalidToken, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise DashboardAuthError("OAuth state 已失效") from exc
        expected = payload.get("state")
        verifier = payload.get("verifier")
        next_path = payload.get("next")
        if not isinstance(expected, str) or not hmac.compare_digest(expected, state):
            raise DashboardAuthError("OAuth state 校验失败")
        if not isinstance(verifier, str) or not isinstance(next_path, str):
            raise DashboardAuthError("OAuth state 内容无效")
        return verifier, safe_next_path(next_path)

    async def exchange_identity(self, code: str, verifier: str) -> dict[str, str]:
        if not code:
            raise DashboardAuthError("授权码缺失")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            token_response = await client.post(
                f"{self._settings.lark_base_url.rstrip('/')}/open-apis/authen/v2/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.lark_app_id,
                    "client_secret": self._settings.lark_app_secret,
                    "code": code,
                    "redirect_uri": self.callback_url,
                    "code_verifier": verifier,
                },
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            if token_payload.get("code", 0) != 0:
                raise DashboardAuthError("飞书授权码交换失败")
            access_token = token_payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise DashboardAuthError("飞书未返回用户凭证")
            user_response = await client.get(
                f"{self._settings.lark_base_url.rstrip('/')}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            user_payload = user_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, DashboardAuthError):
                raise
            raise DashboardAuthError("暂时无法连接飞书登录服务") from exc
        finally:
            if owns_client:
                await client.aclose()

        data: Any = user_payload.get("data", user_payload)
        if not isinstance(data, dict):
            raise DashboardAuthError("飞书用户信息响应无效")
        open_id = data.get("open_id") or data.get("sub")
        if not isinstance(open_id, str) or not open_id:
            raise DashboardAuthError("飞书用户身份缺失")
        return {
            "open_id": open_id,
            "name": str(data.get("name") or "飞书用户")[:128],
            "avatar_url": str(data.get("avatar_url") or data.get("picture") or "")[:1024],
        }

    async def create_session(self, identity: dict[str, str]) -> CreatedSession:
        now = _utcnow()
        expires_at = now + timedelta(seconds=self._settings.dashboard_session_ttl_seconds)
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        async with self._factory() as session:
            context = await IdentityService(
                session,
                currency=self._settings.currency,
                timezone=self._settings.timezone,
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=identity["open_id"],
                display_name=identity.get("name", ""),
            )
            row = DashboardSession(
                token_hash=_digest(session_token),
                csrf_hash=_digest(csrf_token),
                user_open_id=identity["open_id"],
                user_id=context.actor_user_id,
                ledger_id=context.ledger_id,
                display_name=identity.get("name", "")[:128],
                avatar_url=identity.get("avatar_url", "")[:1024],
                expires_at=expires_at,
                last_seen_at=now,
            )
            session.add(row)
            await session.commit()
        return CreatedSession(
            principal=self._principal(row),
            session_token=session_token,
            csrf_token=csrf_token,
        )

    async def authenticate(self, token: str | None) -> DashboardPrincipal:
        if not token:
            raise DashboardAuthError("请先登录")
        now = _utcnow()
        async with self._factory() as session:
            row = await session.scalar(
                select(DashboardSession).where(DashboardSession.token_hash == _digest(token))
            )
            if row is None or row.revoked_at is not None or _aware(row.expires_at) <= now:
                raise DashboardAuthError("登录会话已失效")
            row.last_seen_at = now
            await session.commit()
            return self._principal(row)

    async def verify_csrf(
        self, token: str | None, csrf_cookie: str | None, csrf_header: str | None
    ) -> DashboardPrincipal:
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise DashboardAuthError("CSRF 校验失败")
        principal = await self.authenticate(token)
        async with self._factory() as session:
            row = await session.get(DashboardSession, uuid.UUID(principal.session_id))
            if row is None or not hmac.compare_digest(row.csrf_hash, _digest(csrf_header)):
                raise DashboardAuthError("CSRF 校验失败")
        return principal

    async def revoke(self, token: str | None) -> None:
        if not token:
            return
        async with self._factory() as session:
            row = await session.scalar(
                select(DashboardSession).where(DashboardSession.token_hash == _digest(token))
            )
            if row is not None and row.revoked_at is None:
                row.revoked_at = _utcnow()
                await session.commit()

    def _principal(self, row: DashboardSession) -> DashboardPrincipal:
        if row.user_id is None or row.ledger_id is None:
            raise DashboardAuthError("登录会话缺少内部账本身份")
        role = "ADMIN" if row.user_open_id in self._settings.dashboard_admin_ids else "USER"
        return DashboardPrincipal(
            session_id=str(row.id),
            user_id=row.user_id,
            ledger_id=row.ledger_id,
            user_open_id=row.user_open_id,
            display_name=row.display_name,
            avatar_url=row.avatar_url,
            role=role,
            expires_at=_aware(row.expires_at),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
