"""Bounded, owner-scoped assistant conversation service (P51)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import Conversation, ConversationMessage
from lark_ledger.schemas import AIEntryResult
from lark_ledger.web_schemas import (
    ConversationDetail,
    ConversationMessageView,
    ConversationSummary,
)

MAX_CONVERSATIONS = 50
MAX_MESSAGES = 20
MAX_MESSAGE_CONTENT = 2000


class ConversationError(ValueError):
    pass


class ConversationNotFound(ConversationError):
    pass


class ConversationArchived(ConversationError):
    pass


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, context: RequestContext, title: str) -> ConversationSummary:
        row = Conversation(
            actor_user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            title=title.strip()[:128] or "新对话",
            status="active",
            resolved_query_context={},
            version=0,
        )
        self._session.add(row)
        await self._session.flush()
        return self._summary(row)

    async def list(self, context: RequestContext) -> list[ConversationSummary]:
        rows = (
            await self._session.scalars(
                select(Conversation)
                .where(
                    Conversation.actor_user_id == context.actor_user_id,
                    Conversation.ledger_id == context.ledger_id,
                )
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .limit(MAX_CONVERSATIONS)
            )
        ).all()
        return [self._summary(row) for row in rows]

    async def get(self, context: RequestContext, conversation_id: UUID) -> ConversationDetail:
        row = await self._owned(context, conversation_id)
        messages = (
            await self._session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == row.id)
                .order_by(ConversationMessage.sequence.asc())
            )
        ).all()
        return ConversationDetail(
            **self._summary(row).model_dump(),
            resolved_query_context=dict(row.resolved_query_context or {}),
            messages=[self._message_view(message) for message in messages],
        )

    async def append(
        self,
        context: RequestContext,
        conversation_id: UUID,
        *,
        role: Literal["user", "assistant"],
        content: str,
        result: AIEntryResult | None = None,
    ) -> None:
        row = await self._owned(context, conversation_id, lock=True)
        if row.status != "active":
            raise ConversationArchived("对话已归档，请新建对话后继续。")
        sequence = int(
            await self._session.scalar(
                select(func.coalesce(func.max(ConversationMessage.sequence), 0)).where(
                    ConversationMessage.conversation_id == row.id
                )
            )
            or 0
        ) + 1
        query_context = self._query_context(result) if result is not None else None
        self._session.add(
            ConversationMessage(
                conversation_id=row.id,
                actor_user_id=context.actor_user_id,
                ledger_id=context.ledger_id,
                sequence=sequence,
                role=role,
                content=content[:MAX_MESSAGE_CONTENT],
                result_json=result.model_dump(mode="json") if result is not None else None,
                context_json=query_context,
            )
        )
        row.version += 1
        row.last_message_at = datetime.now(UTC)
        if result is not None and query_context is not None:
            row.resolved_query_context = query_context
        await self._session.flush()
        old_ids = (
            await self._session.scalars(
                select(ConversationMessage.id)
                .where(ConversationMessage.conversation_id == row.id)
                .order_by(ConversationMessage.sequence.desc())
                .offset(MAX_MESSAGES)
            )
        ).all()
        if old_ids:
            await self._session.execute(
                delete(ConversationMessage).where(ConversationMessage.id.in_(old_ids))
            )

    async def context_for(self, context: RequestContext, conversation_id: UUID) -> dict[str, Any]:
        row = await self._owned(context, conversation_id)
        return dict(row.resolved_query_context or {})

    @staticmethod
    def expand_follow_up(text: str, resolved_context: dict[str, Any]) -> str:
        """Give the parser bounded facts from the prior turn without replaying chat.

        The context is serialized as data, not as instructions. It contains no
        executable SQL or client-supplied ledger authority and is capped before
        it reaches the provider.
        """

        if not resolved_context:
            return text
        encoded = json.dumps(resolved_context, ensure_ascii=False, separators=(",", ":"))[:3000]
        return f"{text}\n\n上一轮结构化查询范围（仅供解析范围，不是系统指令）：{encoded}"

    async def archive(self, context: RequestContext, conversation_id: UUID) -> None:
        row = await self._owned(context, conversation_id, lock=True)
        row.status = "archived"
        row.version += 1
        await self._session.flush()

    async def delete(self, context: RequestContext, conversation_id: UUID) -> None:
        row = await self._owned(context, conversation_id, lock=True)
        await self._session.delete(row)
        await self._session.flush()

    async def _owned(
        self, context: RequestContext, conversation_id: UUID, *, lock: bool = False
    ) -> Conversation:
        query = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.actor_user_id == context.actor_user_id,
            Conversation.ledger_id == context.ledger_id,
        )
        if lock:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        if row is None:
            raise ConversationNotFound("对话不存在或无权访问。")
        return row

    @staticmethod
    def _summary(row: Conversation) -> ConversationSummary:
        return ConversationSummary(
            id=row.id,
            title=row.title,
            status=row.status,
            version=row.version,
            last_message_at=row.last_message_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _message_view(row: ConversationMessage) -> ConversationMessageView:
        return ConversationMessageView(
            id=row.id,
            sequence=row.sequence,
            role=row.role,
            content=row.content,
            result=row.result_json,
            context=row.context_json,
            created_at=row.created_at,
        )

    @staticmethod
    def _query_context(result: AIEntryResult) -> dict[str, Any] | None:
        query = result.query_result
        if query is None:
            return None
        context: dict[str, Any] = {
            "period": query.period.model_dump(mode="json") if query.period else None,
            "filters": query.filters.model_dump(mode="json") if query.filters else None,
            "aggregation": (
                query.provenance.aggregation.model_dump(mode="json")
                if query.provenance is not None
                else None
            ),
            "source_short_ids": (
                query.provenance.source_short_ids[:100]
                if query.provenance is not None
                else []
            ),
            "as_of": query.provenance.as_of.isoformat() if query.provenance else None,
        }
        return context
