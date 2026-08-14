import json
import logging
from typing import Annotated, Any, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from lark_ledger.buildinfo import resolve_build_info
from lark_ledger.config import EventMode, Settings, get_settings
from lark_ledger.readiness import ReadinessService, startup_incomplete_response
from lark_ledger.services.events import EventService
from lark_ledger.services.feishu import decrypt_event, verify_signature
from lark_ledger.system_status import SystemStatusService

router = APIRouter()

logger = logging.getLogger(__name__)


def get_event_service(request: Request) -> EventService:
    return cast(EventService, request.app.state.event_service)


@router.get("/version")
async def version(request: Request) -> dict[str, str]:
    """Return the running build identity (never secrets).

    Public contract: ``version`` / ``git_sha`` / ``build_time`` resolved from
    image build args with safe in-repo fallbacks. No environment variables,
    database URLs, or tokens are exposed.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return resolve_build_info(settings).to_dict()


@router.get("/ops/status")
async def ops_status(request: Request) -> JSONResponse:
    """Aggregated operational status: backlog + worker heartbeat + build id.

    Deliberately bounded and redacted: only per-status row counts (no rows, no
    ledger/user dimensions), worker loop timestamps (no owner ids, no hostnames)
    and the public build identity. A failing observability query degrades to
    ``status: "unavailable"`` instead of failing the whole endpoint.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    session_factory = getattr(request.app.state, "session_factory", None)
    worker_heartbeats = _worker_heartbeat_payload(request)
    backlog: dict[str, Any]
    if session_factory is None:
        backlog = {"status": "unavailable", "reason": "startup_incomplete"}
    else:
        try:
            backlog = await SystemStatusService(session_factory).aggregate()
        except Exception as exc:
            logger.warning(
                "ops status backlog aggregate failed error_code=%s",
                type(exc).__name__,
            )
            backlog = {"status": "unavailable", "reason": "aggregate_unavailable"}
    dead_warning = _dead_warning(settings, backlog)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "build": resolve_build_info(settings).to_dict(),
            "backlog": backlog,
            "workers": worker_heartbeats,
            **({"dead_warning": True} if dead_warning else {}),
        },
    )


def _dead_warning(settings: Settings, backlog: dict[str, Any]) -> bool:
    """Aggregate dead-count threshold check (P44, opt-in, readiness-agnostic).

    Only surfaces an advisory flag; it never changes readiness (a backlog stays
    ``degraded``-style at most and never turns into a 503).
    """
    threshold = settings.ops_dead_warning_threshold
    if threshold <= 0:
        return False
    total = 0
    for key in ("events", "outbox"):
        section = backlog.get(key) if isinstance(backlog, dict) else None
        if isinstance(section, dict):
            total += int(section.get("dead") or 0)
    return total >= threshold


def _worker_heartbeat_payload(request: Request) -> dict[str, Any]:
    """Redacted worker loop state: timestamps only, never owner ids."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    components = (
        ("event_worker", settings.worker_enabled),
        ("reply_worker", settings.reply_worker_enabled),
        ("cleanup_worker", settings.cleanup_enabled),
        ("recurring_worker", settings.recurring_enabled),
    )
    payload: dict[str, Any] = {}
    for component, enabled in components:
        if not enabled:
            payload[component] = {"status": "disabled"}
            continue
        worker = getattr(request.app.state, component, None)
        if worker is None:
            payload[component] = {"status": "not_started"}
            continue
        try:
            snapshot = worker.health_snapshot()
        except Exception as exc:
            logger.warning(
                "ops status worker snapshot failed component=%s error_code=%s",
                component,
                type(exc).__name__,
            )
            payload[component] = {"status": "unknown", "reason": "snapshot_unavailable"}
            continue
        payload[component] = {
            key: snapshot.get(key)
            for key in (
                "started",
                "running",
                "stopping",
                "task_done",
                "task_exception",
                "last_sweep_at",
                "last_success_at",
                "last_error_at",
                "sweeps",
                "processed",
            )
        }
    receiver = getattr(request.app.state, "long_connection", None)
    if receiver is None:
        payload["receiver"] = {"status": "disabled"}
    else:
        try:
            snapshot = receiver.health_snapshot()
        except Exception as exc:
            logger.warning(
                "ops status receiver snapshot failed error_code=%s",
                type(exc).__name__,
            )
            payload["receiver"] = {"status": "unknown", "reason": "snapshot_unavailable"}
        else:
            payload["receiver"] = {
                key: snapshot.get(key)
                for key in (
                    "started",
                    "running",
                    "stopping",
                    "task_done",
                    "task_exception",
                    "connection_status",
                    "last_event_at",
                    "last_error_at",
                )
            }
    return payload


@router.get("/healthz")
async def health(request: Request) -> dict[str, str]:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    receiver = getattr(request.app.state, "long_connection", None)
    connection_status = receiver.status if receiver is not None else "disabled"
    return {
        "status": "ok",
        "event_mode": settings.event_mode.value,
        "long_connection": connection_status,
    }


@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    service = cast(ReadinessService | None, getattr(request.app.state, "readiness", None))
    if service is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=startup_incomplete_response(),
        )
    payload = await service.check(request.app.state)
    response_status = (
        status.HTTP_200_OK
        if payload["status"] == "ready"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(status_code=response_status, content=payload)


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

    event_id = str(header.get("event_id", ""))
    event = payload.get("event")
    event_type = header.get("event_type")
    if event_type == "card.action.trigger":
        if not event_id:
            raise HTTPException(status_code=400, detail="missing event_id")
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="missing event")
        card_service = getattr(request.app.state, "card_action_service", None)
        if card_service is None:
            return {"code": 0}
        background_tasks.add_task(card_service.handle_action, event_id, event)
        return {"code": 0}
    if event_type != "im.message.receive_v1":
        return {"code": 0}

    if not event_id:
        raise HTTPException(status_code=400, detail="missing event_id")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="missing event")
    background_tasks.add_task(
        event_service.handle_safely,
        event_id,
        event,
        transport="webhook",
    )
    return {"code": 0}
