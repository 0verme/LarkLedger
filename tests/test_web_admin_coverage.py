"""Direct service-level coverage for ``WebAdminQueryService`` read-model branches.

The dashboard HTTP tests exercise the happy path; these tests drive the
remaining edge branches (masking, legacy status remap, pagination, dead
summaries) directly against the service so coverage is attributable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.models import ProcessedEvent, ReplyOutbox
from lark_ledger.services.web_admin import WebAdminQueryService, _mask_message_id


def _event(event_id: str, *, status: str = "dead") -> ProcessedEvent:
    return ProcessedEvent(
        event_id=event_id,
        status=status,
        transport="webhook",
        attempt_count=2,
        source_message_id=f"om_{event_id}",
        user_open_id="ou_private",
        received_at=datetime.now(UTC) - timedelta(minutes=1),
    )


def _reply(event_id: str, *, status: str = "dead") -> ReplyOutbox:
    return ReplyOutbox(
        id=uuid4(),
        event_id=event_id,
        message_id=f"om_reply_{event_id}",
        reply_type="text",
        sequence=0,
        payload_json={"text": "private reply"},
        status=status,
        attempt_count=1,
    )


def test_mask_message_id_empty_and_short_values() -> None:
    assert _mask_message_id(None) is None
    assert _mask_message_id("") is None
    assert _mask_message_id("om123") == "om…23"
    assert _mask_message_id("om_1234567890") == "om_12…7890"


async def test_events_legacy_status_remap_and_pagination(session: AsyncSession) -> None:
    session.add_all(
        [
            _event("evt-1", status="legacy_succeeded"),
            _event("evt-2"),
            _event("evt-3"),
            _event("evt-4"),
        ]
    )
    await session.commit()

    service = WebAdminQueryService(session)
    remapped = await service.events(status="legacy", page=1, page_size=2)
    assert remapped.total == 1
    assert remapped.pages == 1
    assert [item.event_id for item in remapped.items] == ["evt-1"]

    second_page = await service.events(status="dead", page=2, page_size=2)
    assert second_page.total == 3
    assert second_page.pages == 2
    assert len(second_page.items) == 1

    unfiltered = await service.events(status=None, page=1, page_size=10)
    assert unfiltered.total == 4


async def test_outbox_rows_and_dead_summary(session: AsyncSession) -> None:
    session.add_all(
        [
            _event("evt-dead"),
            _event("evt-sent", status="succeeded"),
            _reply("evt-dead"),
            _reply("evt-sent", status="sent"),
        ]
    )
    await session.commit()

    service = WebAdminQueryService(session)
    outbox = await service.outbox(status="dead", page=1, page_size=1)
    assert outbox.total == 1
    assert outbox.pages == 1
    assert outbox.items[0].event_id == "evt-dead"

    summary = await service.dead_summary(limit=1)
    assert summary.event_count == 1
    assert summary.reply_count == 1
    assert [item.event_id for item in summary.latest_events] == ["evt-dead"]
    assert [item.event_id for item in summary.latest_replies] == ["evt-dead"]
