import json
from typing import Annotated, Any, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status

from lark_ledger.config import EventMode, Settings, get_settings
from lark_ledger.services.events import EventService
from lark_ledger.services.feishu import decrypt_event, verify_signature

router = APIRouter()


def get_event_service(request: Request) -> EventService:
    return cast(EventService, request.app.state.event_service)


@router.get("/healthz")
async def health(request: Request) -> dict[str, str]:
    settings = get_settings()
    receiver = getattr(request.app.state, "long_connection", None)
    connection_status = receiver.status if receiver is not None else "disabled"
    return {
        "status": "ok",
        "event_mode": settings.event_mode.value,
        "long_connection": connection_status,
    }


@router.post("/webhooks/feishu")
async def feishu_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    event_service: Annotated[EventService, Depends(get_event_service)],
    x_lark_request_timestamp: Annotated[str, Header()] = "",
    x_lark_request_nonce: Annotated[str, Header()] = "",
    x_lark_signature: Annotated[str, Header()] = "",
) -> dict[str, Any]:
    if settings.event_mode is not EventMode.WEBHOOK:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook mode disabled")
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
    event = payload.get("event")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="missing event")
    background_tasks.add_task(
        event_service.handle_safely,
        event_id,
        event,
        transport="webhook",
    )
    return {"code": 0}
