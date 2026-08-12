from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.client_schemas import (
    ClientCredentialCreated,
    ClientCredentialCreateRequest,
    ClientCredentialView,
)
from lark_ledger.context import RequestContext
from lark_ledger.models import (
    ChannelIdentity,
    ClientCredential,
    ClientSecurityAudit,
    User,
    UserStatus,
)
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.ledger_management import LedgerManagementService

TOKEN_PREFIX = "llv1_"


class ClientAuthenticationError(PermissionError):
    pass


class ClientScopeError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ClientPrincipal:
    credential_id: uuid.UUID
    context: RequestContext
    display_name: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ClientScopeError("credential scope does not allow this operation")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def credential_view(row: ClientCredential) -> ClientCredentialView:
    return ClientCredentialView(
        id=str(row.id),
        name=row.name,
        token_prefix=row.token_prefix,
        scopes=row.scopes.split(),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


class ClientCredentialService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        currency: str,
        timezone: str,
    ) -> None:
        self._factory = session_factory
        self._currency = currency
        self._timezone = timezone

    async def authenticate(self, token: str | None) -> ClientPrincipal:
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) < 40:
            raise ClientAuthenticationError("valid bearer credential required")
        now = datetime.now(UTC)
        async with self._factory() as session:
            row = await session.scalar(
                select(ClientCredential).where(ClientCredential.token_digest == token_digest(token))
            )
            if row is None or row.revoked_at is not None:
                raise ClientAuthenticationError("valid bearer credential required")
            expires_at = row.expires_at
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= now:
                    raise ClientAuthenticationError("credential expired")
            user = await session.get(User, row.user_id)
            if user is None or user.status != UserStatus.ACTIVE.value:
                raise ClientAuthenticationError("valid bearer credential required")
            ledger_id = row.current_ledger_id
            if ledger_id is None or not await LedgerAuthorizationService(session).can_access(
                user.id, ledger_id
            ):
                ledger = await LedgerManagementService(
                    session, currency=self._currency, timezone=self._timezone
                ).get_default(user.id)
                ledger_id = ledger.id
                row.current_ledger_id = ledger_id
            external_subject_id = await session.scalar(
                select(ChannelIdentity.external_subject_id)
                .where(
                    ChannelIdentity.user_id == user.id,
                    ChannelIdentity.channel == "feishu",
                )
                .order_by(ChannelIdentity.created_at)
                .limit(1)
            )
            row.last_used_at = now
            await session.commit()
            return ClientPrincipal(
                credential_id=row.id,
                context=RequestContext(
                    actor_user_id=user.id,
                    ledger_id=ledger_id,
                    source_channel="client_api",
                    external_subject_id=external_subject_id,
                    actor_kind="client",
                ),
                display_name=user.display_name,
                scopes=frozenset(row.scopes.split()),
            )

    @staticmethod
    async def select_ledger(
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        credential_id: uuid.UUID,
        ledger_id: uuid.UUID,
    ) -> None:
        await LedgerAuthorizationService(session).get_accessible(user_id, ledger_id)
        row = await session.scalar(
            select(ClientCredential).where(
                ClientCredential.id == credential_id,
                ClientCredential.user_id == user_id,
                ClientCredential.revoked_at.is_(None),
            )
        )
        if row is None:
            raise ClientAuthenticationError("valid bearer credential required")
        row.current_ledger_id = ledger_id
        await session.flush()

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        current_ledger_id: uuid.UUID,
        request: ClientCredentialCreateRequest,
    ) -> ClientCredentialCreated:
        now = datetime.now(UTC)
        if request.expires_at is not None:
            expires_at = request.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                raise ValueError("expires_at must be in the future")
        name = request.name.strip()
        if not name:
            raise ValueError("credential name must not be blank")
        token = TOKEN_PREFIX + secrets.token_urlsafe(48)
        row = ClientCredential(
            user_id=user_id,
            current_ledger_id=current_ledger_id,
            name=name,
            token_digest=token_digest(token),
            token_prefix=token[:12],
            scopes=" ".join(sorted(set(request.scopes))),
            expires_at=request.expires_at,
        )
        session.add(row)
        await session.flush()
        session.add(
            ClientSecurityAudit(
                actor_user_id=user_id,
                credential_id=row.id,
                action="credential.create",
                outcome="succeeded",
            )
        )
        await session.commit()
        return ClientCredentialCreated(token=token, **credential_view(row).model_dump())

    @staticmethod
    async def list_for_user(
        session: AsyncSession, user_id: uuid.UUID
    ) -> list[ClientCredentialView]:
        rows = (
            await session.scalars(
                select(ClientCredential)
                .where(ClientCredential.user_id == user_id)
                .order_by(ClientCredential.created_at.desc())
            )
        ).all()
        return [credential_view(row) for row in rows]

    @staticmethod
    async def revoke(
        session: AsyncSession, *, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> None:
        row = await session.scalar(
            select(ClientCredential).where(
                ClientCredential.id == credential_id,
                ClientCredential.user_id == user_id,
            )
        )
        if row is None:
            raise LookupError("credential not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            session.add(
                ClientSecurityAudit(
                    actor_user_id=user_id,
                    credential_id=row.id,
                    action="credential.revoke",
                    outcome="succeeded",
                )
            )
        await session.commit()
