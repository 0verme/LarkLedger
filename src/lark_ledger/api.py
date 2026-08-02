import json
import logging
from typing import Annotated, Any, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.config import Settings, get_settings
from lark_ledger.db import get_session
from lark_ledger.models import ProcessedEvent
from lark_ledger.services.feishu import MessageProcessor, decrypt_event, verify_signature

logger = logging.getLogger(__name__)
router = APIRouter()


def get_processor(request: Request) -> MessageProcessor:
    return cast(MessageProcessor, request.app.state.processor)


async def run_processor(processor: MessageProcessor, event: dict[str, Any]) -> None:
    try:
        await processor.process(event)
    except Exception:
        logger.exception("failed to process Feishu message")


@router.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/webhooks/feishu")
async def feishu_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    processor: Annotated[MessageProcessor, Depends(get_processor)],
    x_lark_request_timestamp: Annotated[str, Header()] = "",
    x_lark_request_nonce: Annotated[str, Header()] = "",
    x_lark_signature: Annotated[str, Header()] = "",
) -> dict[str, Any]:
    raw_body = await request.body()
    if settings.lark_encrypt_key and not verify_signature(
        raw_body,
        x_lark_request_timestamp,
        x_lark_request_nonce,
        x_lark_signature,
        settings.lark_encrypt_key,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    try:
        payload = json.loads(raw_body)
        if "encrypt" in payload:
            if not settings.lark_encrypt_key:
                raise HTTPException(status_code=400, detail="encrypted event without key")
            payload = decrypt_event(str(payload["encrypt"]), settings.lark_encrypt_key)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid event payload") from exc

    header = payload.get("header", {})
    token = header.get("token") or payload.get("token")
    if settings.lark_verification_token and token != settings.lark_verification_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if header.get("event_type") != "im.message.receive_v1":
        return {"code": 0}

    event_id = str(header.get("event_id", ""))
    if not event_id:
        raise HTTPException(status_code=400, detail="missing event_id")
    session.add(ProcessedEvent(event_id=event_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return {"code": 0}
    background_tasks.add_task(run_processor, processor, payload["event"])
    return {"code": 0}
