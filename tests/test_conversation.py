from uuid import uuid4

import pytest

from lark_ledger.context import RequestContext
from lark_ledger.services.conversation import (
    MAX_MESSAGES,
    ConversationNotFound,
    ConversationService,
)


def ctx() -> RequestContext:
    return RequestContext(actor_user_id=uuid4(), ledger_id=uuid4(), source_channel="web")


@pytest.mark.asyncio
async def test_conversation_is_owned_and_history_is_bounded(session) -> None:
    owner = ctx()
    other = ctx()
    service = ConversationService(session)
    created = await service.create(owner, "本月支出")
    for index in range(MAX_MESSAGES + 4):
        await service.append(owner, created.id, role="user", content=f"问题 {index}")
    detail = await service.get(owner, created.id)
    assert len(detail.messages) == MAX_MESSAGES
    assert detail.messages[0].content == "问题 4"
    with pytest.raises(ConversationNotFound):
        await service.get(other, created.id)


def test_follow_up_context_is_bounded_data_not_instruction() -> None:
    expanded = ConversationService.expand_follow_up(
        "把那几笔列出来", {"period": {"start": "2026-08-01"}, "filters": {}}
    )
    assert "把那几笔列出来" in expanded
    assert "上一轮结构化查询范围" in expanded
    assert len(expanded) < 3100
