import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.event_payload import (
    PAYLOAD_VERSION,
    REPLAY_SAFETY_VERSION,
    build_stored_payload,
)
from lark_ledger.models import ProcessedEvent, ReplyOutbox
from lark_ledger.services.event_replay import EventReplayService
from lark_ledger.services.replay import OutboxReplayService
from lark_ledger.services.web_admin import WebAdminQueryService

pytestmark = pytest.mark.postgres


async def test_admin_queries_and_both_replay_paths_on_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    event_id = "evt-web-replay-pg"
    message_id = "om_sensitive_pg_event"
    event_payload = {
        "sender": {"sender_id": {"open_id": "ou_private"}},
        "message": {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": "private financial text"}),
        },
    }
    reply_event_id = "evt-web-result-pg"
    reply_id = uuid.uuid4()
    async with postgres_session_factory() as session:
        session.add_all(
            [
                ProcessedEvent(
                    event_id=event_id,
                    payload_json=build_stored_payload(
                        event_id,
                        event_payload,
                        transport="webhook",
                        received_at=now - timedelta(minutes=2),
                    ),
                    payload_version=PAYLOAD_VERSION,
                    replay_safety_version=REPLAY_SAFETY_VERSION,
                    transport="webhook",
                    status="dead",
                    attempt_count=3,
                    source_message_id=message_id,
                    user_open_id="ou_private",
                    received_at=now - timedelta(minutes=2),
                    last_error_code="TemporaryFailure",
                ),
                ProcessedEvent(
                    event_id=reply_event_id,
                    status="succeeded",
                    attempt_count=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            ReplyOutbox(
                id=reply_id,
                event_id=reply_event_id,
                message_id="om_private_target",
                reply_type="text",
                sequence=0,
                payload_json={"text": "committed result"},
                status="dead",
            )
        )
        await session.commit()

    async with postgres_session_factory() as session:
        query = WebAdminQueryService(session)
        events = await query.events(status="dead", page=1, page_size=25)
        replies = await query.outbox(status="dead", page=1, page_size=25)
        assert events.total == 1
        assert events.items[0].source_message_id == "om_se…vent"
        assert replies.total == 1

    preflight = await EventReplayService(postgres_session_factory).replay(
        event_id, operator="ou_admin", reason="validated transient failure"
    )
    assert preflight.preflight.eligible is True
    executed = await EventReplayService(postgres_session_factory).replay(
        event_id,
        operator="ou_admin",
        reason="validated transient failure",
        execute=True,
    )
    result = await OutboxReplayService(postgres_session_factory).replay_ids([reply_id])
    assert executed.outcome == "requeued"
    assert result.reset == 1

    async with postgres_session_factory() as session:
        event = await session.get(ProcessedEvent, event_id)
        reply = await session.get(ReplyOutbox, reply_id)
        assert event is not None and event.status == "received"
        assert reply is not None and reply.status == "pending"
