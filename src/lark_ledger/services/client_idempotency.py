from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import ClientIdempotencyRecord


class IdempotencyConflictError(ValueError):
    pass


class IdempotencyInProgressError(RuntimeError):
    pass


def request_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ClientIdempotencyService:
    def __init__(self, session: AsyncSession, *, ttl: timedelta = timedelta(days=7)) -> None:
        self._session = session
        self._ttl = ttl

    async def execute(
        self,
        context: RequestContext,
        *,
        operation: str,
        key: str,
        payload: Any,
        callback: Callable[[ClientIdempotencyRecord], Awaitable[dict[str, Any]]],
        response_status: int = 200,
    ) -> tuple[dict[str, Any], bool]:
        normalized_key = key.strip()
        if not normalized_key or len(normalized_key) > 128:
            raise ValueError("Idempotency-Key must contain 1 to 128 characters")
        digest = request_digest(payload)
        now = datetime.now(UTC)
        existing = await self._session.scalar(
            select(ClientIdempotencyRecord)
            .where(
                ClientIdempotencyRecord.actor_user_id == context.actor_user_id,
                ClientIdempotencyRecord.operation == operation,
                ClientIdempotencyRecord.ledger_id == context.ledger_id,
                ClientIdempotencyRecord.idempotency_key == normalized_key,
            )
            .with_for_update()
        )
        if existing is not None:
            expires_at = existing.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                await self._session.delete(existing)
                await self._session.flush()
            elif existing.request_digest != digest:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different request"
                )
            elif existing.response_json is not None:
                return existing.response_json, True
            else:
                raise IdempotencyInProgressError("idempotent request is still in progress")
        record = ClientIdempotencyRecord(
            actor_user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            operation=operation,
            idempotency_key=normalized_key,
            request_digest=digest,
            expires_at=now + self._ttl,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError:
            # A concurrent request can win the scoped unique constraint after
            # our initial read. Roll back the empty attempt, then replay the
            # committed winner or return the same stable conflict semantics.
            await self._session.rollback()
            winner = await self._session.scalar(
                select(ClientIdempotencyRecord).where(
                    ClientIdempotencyRecord.actor_user_id == context.actor_user_id,
                    ClientIdempotencyRecord.operation == operation,
                    ClientIdempotencyRecord.ledger_id == context.ledger_id,
                    ClientIdempotencyRecord.idempotency_key == normalized_key,
                )
            )
            if winner is None:
                raise IdempotencyInProgressError(
                    "idempotent request could not be claimed"
                ) from None
            if winner.request_digest != digest:
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different request"
                ) from None
            if winner.response_json is None:
                raise IdempotencyInProgressError(
                    "idempotent request is still in progress"
                ) from None
            return winner.response_json, True
        response = await callback(record)
        record.response_status = response_status
        record.response_json = response
        record.completed_at = datetime.now(UTC)
        await self._session.commit()
        return response, False

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        result = await self._session.execute(
            delete(ClientIdempotencyRecord).where(
                ClientIdempotencyRecord.expires_at <= (now or datetime.now(UTC))
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
